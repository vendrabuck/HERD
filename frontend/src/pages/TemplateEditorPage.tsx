import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import toast from "react-hot-toast";
import {
  useTemplate,
  useCreateTemplate,
  useUpdateTemplate,
} from "@/api/templates";
import { useDrivers } from "@/api/drivers";
import { useAIStatus, useSuggestTemplateIdentity } from "@/api/ai";
import { useAuthStore } from "@/stores/authStore";
import { FieldRow } from "@/components/templates/FieldRow";
import type {
  FieldDefinition,
  SectionDefinition,
} from "@/types/template.types";

const DEFAULT_FIELD: FieldDefinition = {
  key: "",
  label: "",
  type: "string",
  required: false,
};

const DEFAULT_SECTIONS: SectionDefinition[] = [
  { name: "Primary Values", fields: [] },
  { name: "Secondary Values", fields: [] },
];

const FIELD_TYPE_LABELS: Record<string, string> = {
  string: "String",
  number: "Number",
  boolean: "Boolean",
  dropdown: "Dropdown",
};

export function TemplateEditorPage() {
  const { id } = useParams<{ id: string }>();
  const isNew = id === "new" || !id;
  const navigate = useNavigate();

  const { data: existing, isLoading } = useTemplate(isNew ? undefined : id);
  const createTemplate = useCreateTemplate();
  const updateTemplate = useUpdateTemplate();
  const { data: drivers } = useDrivers();
  const { data: aiStatus } = useAIStatus();
  const suggestIdentity = useSuggestTemplateIdentity();
  const user = useAuthStore((s) => s.user);
  const isAdmin = user?.role === "admin" || user?.role === "superadmin";

  const [editing, setEditing] = useState(isNew);
  const [name, setName] = useState("");
  const [templateType, setTemplateType] = useState<"device" | "port">("device");
  const [driverId, setDriverId] = useState<string | null>(null);
  const [exclusive, setExclusive] = useState(true);
  const [description, setDescription] = useState("");
  const [icon, setIcon] = useState<string | null>(null);
  const [vendor, setVendor] = useState("");
  const [model, setModel] = useState("");
  const [partNumber, setPartNumber] = useState("");
  // Blank string = inherit / none; numeric string = template default for new devices.
  const [pollInterval, setPollInterval] = useState("");
  const [sections, setSections] = useState<SectionDefinition[]>(DEFAULT_SECTIONS);
  const [syncedId, setSyncedId] = useState<string | undefined>();

  if (existing && existing.id !== syncedId) {
    setSyncedId(existing.id);
    setName(existing.name);
    setTemplateType(existing.template_type as "device" | "port");
    setDriverId(existing.driver_id);
    setExclusive(existing.exclusive);
    setDescription(existing.description ?? "");
    setIcon(existing.icon);
    setVendor(existing.vendor === "unknown" ? "" : existing.vendor);
    setModel(existing.model === "unknown" ? "" : existing.model);
    setPartNumber(existing.part_number ?? "");
    setPollInterval(
      existing.poll_interval_seconds !== null
        ? String(existing.poll_interval_seconds)
        : "",
    );
    setSections(existing.sections);
  }

  const identityRequired = templateType === "device";
  const identityValid =
    !identityRequired || (vendor.trim() !== "" && model.trim() !== "");

  const handleSuggestIdentity = async () => {
    if (!name.trim()) {
      toast.error("Enter a template name first; the AI uses it as the main signal.");
      return;
    }
    try {
      const suggestion = await suggestIdentity.mutateAsync({
        name: name.trim(),
        description: description.trim() || null,
        sections: sections as unknown as Array<Record<string, unknown>>,
      });
      setVendor(suggestion.vendor);
      setModel(suggestion.model);
      if (suggestion.part_number) {
        setPartNumber(suggestion.part_number);
      }
      toast.success(
        `Suggested: ${suggestion.vendor}, ${suggestion.model} (confidence: ${suggestion.confidence})`,
      );
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Failed to suggest identity";
      toast.error(msg);
    }
  };

  const handleIconUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    if (file.size > 256 * 1024) {
      toast.error("Icon must be under 256 KB");
      return;
    }
    const reader = new FileReader();
    reader.onload = () => setIcon(reader.result as string);
    reader.readAsDataURL(file);
  };

  const updateSection = (idx: number, patch: Partial<SectionDefinition>) => {
    setSections((prev) =>
      prev.map((s, i) => (i === idx ? { ...s, ...patch } : s)),
    );
  };

  const removeSection = (idx: number) => {
    setSections((prev) => prev.filter((_, i) => i !== idx));
  };

  const addSection = () => {
    setSections((prev) => [...prev, { name: "", fields: [] }]);
  };

  const updateField = (
    sectionIdx: number,
    fieldIdx: number,
    field: FieldDefinition,
  ) => {
    setSections((prev) =>
      prev.map((s, si) =>
        si === sectionIdx
          ? {
              ...s,
              fields: s.fields.map((f, fi) => (fi === fieldIdx ? field : f)),
            }
          : s,
      ),
    );
  };

  const removeField = (sectionIdx: number, fieldIdx: number) => {
    setSections((prev) =>
      prev.map((s, si) =>
        si === sectionIdx
          ? { ...s, fields: s.fields.filter((_, fi) => fi !== fieldIdx) }
          : s,
      ),
    );
  };

  const addField = (sectionIdx: number) => {
    setSections((prev) =>
      prev.map((s, si) =>
        si === sectionIdx
          ? { ...s, fields: [...s.fields, { ...DEFAULT_FIELD }] }
          : s,
      ),
    );
  };

  const handleSave = async () => {
    if (!name.trim()) {
      toast.error("Name is required");
      return;
    }
    if (templateType === "device" && !driverId) {
      toast.error("Device templates must have a driver");
      return;
    }
    if (identityRequired && (!vendor.trim() || !model.trim())) {
      toast.error("Vendor and Model are required for device templates");
      return;
    }
    if (sections.length === 0) {
      toast.error("At least one section is required");
      return;
    }

    const trimmedVendor = vendor.trim();
    const trimmedModel = model.trim();
    const trimmedPartNumber = partNumber.trim();

    const trimmedPollInterval = pollInterval.trim();
    let pollIntervalValue: number | null = null;
    if (trimmedPollInterval !== "") {
      const n = Number(trimmedPollInterval);
      if (!Number.isInteger(n) || n < 30) {
        toast.error("Poll interval must be a whole number >= 30 (or blank for none)");
        return;
      }
      pollIntervalValue = n;
    }

    try {
      if (isNew) {
        await createTemplate.mutateAsync({
          name: name.trim(),
          template_type: templateType,
          driver_id: templateType === "device" ? driverId : null,
          exclusive,
          icon,
          description: description.trim() || undefined,
          vendor: trimmedVendor || undefined,
          model: trimmedModel || undefined,
          part_number: trimmedPartNumber || null,
          sections,
          poll_interval_seconds: pollIntervalValue,
        });
        toast.success("Template created");
        navigate("/templates");
      } else {
        await updateTemplate.mutateAsync({
          id: id!,
          data: {
            name: name.trim(),
            driver_id: templateType === "device" ? driverId : null,
            exclusive,
            icon,
            description: description.trim() || undefined,
            vendor: trimmedVendor || undefined,
            model: trimmedModel || undefined,
            part_number: trimmedPartNumber || null,
            sections,
            poll_interval_seconds: pollIntervalValue,
          },
        });
        toast.success("Template updated");
        setEditing(false);
      }
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ?? "Failed to save template";
      toast.error(msg);
    }
  };

  const handleCancel = () => {
    if (isNew) {
      navigate("/templates");
      return;
    }
    if (existing) {
      setName(existing.name);
      setTemplateType(existing.template_type as "device" | "port");
      setDriverId(existing.driver_id);
      setExclusive(existing.exclusive);
      setDescription(existing.description ?? "");
      setIcon(existing.icon);
      setVendor(existing.vendor === "unknown" ? "" : existing.vendor);
      setModel(existing.model === "unknown" ? "" : existing.model);
      setPartNumber(existing.part_number ?? "");
      setPollInterval(
        existing.poll_interval_seconds !== null
          ? String(existing.poll_interval_seconds)
          : "",
      );
      setSections(existing.sections);
    }
    setEditing(false);
  };

  if (!isNew && isLoading) {
    return (
      <div className="h-full flex items-center justify-center">
        <p className="text-sm text-gray-400">Loading template...</p>
      </div>
    );
  }

  if (!isNew && !existing && !isLoading) {
    return (
      <div className="h-full flex items-center justify-center">
        <p className="text-sm text-red-500">Template not found</p>
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-4xl mx-auto px-6 py-6 space-y-6">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold text-gray-900">
            {isNew ? "Create Template" : editing ? "Edit Template" : "Template Details"}
          </h2>
          <div className="flex gap-2">
            <button
              onClick={() => navigate("/templates")}
              className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 rounded-lg hover:bg-gray-200 transition-colors"
            >
              Back
            </button>
            {editing ? (
              <>
                {!isNew && (
                  <button
                    onClick={handleCancel}
                    className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 rounded-lg hover:bg-gray-200 transition-colors"
                  >
                    Cancel
                  </button>
                )}
                <button
                  onClick={handleSave}
                  disabled={
                    createTemplate.isPending ||
                    updateTemplate.isPending ||
                    !identityValid
                  }
                  className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
                >
                  {createTemplate.isPending || updateTemplate.isPending
                    ? "Saving..."
                    : "Save"}
                </button>
              </>
            ) : (
              isAdmin && (
                <button
                  onClick={() => setEditing(true)}
                  className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors"
                >
                  Edit
                </button>
              )
            )}
          </div>
        </div>

        {editing ? (
          <>
            <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-4">
              <div className="grid grid-cols-3 gap-4">
                <div>
                  <label
                    htmlFor="tmpl-name"
                    className="block text-sm font-medium text-gray-700 mb-1"
                  >
                    Name
                  </label>
                  <input
                    id="tmpl-name"
                    type="text"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                  />
                </div>
                <div>
                  <label
                    htmlFor="tmpl-type"
                    className="block text-sm font-medium text-gray-700 mb-1"
                  >
                    Type
                  </label>
                  <select
                    id="tmpl-type"
                    value={templateType}
                    onChange={(e) => {
                      const val = e.target.value as "device" | "port";
                      setTemplateType(val);
                      if (val !== "device") setDriverId(null);
                    }}
                    disabled={!isNew}
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-100 disabled:text-gray-500"
                  >
                    <option value="device">Device</option>
                    <option value="port">Port</option>
                  </select>
                </div>
                <div>
                  <label
                    htmlFor="tmpl-icon"
                    className="block text-sm font-medium text-gray-700 mb-1"
                  >
                    Icon
                  </label>
                  <div className="flex items-center gap-3">
                    {icon && (
                      <img
                        src={icon}
                        alt="Template icon"
                        className="w-10 h-10 object-contain rounded border border-gray-200"
                      />
                    )}
                    <input
                      id="tmpl-icon"
                      type="file"
                      accept="image/jpeg,image/png"
                      onChange={handleIconUpload}
                      className="text-sm text-gray-500 file:mr-2 file:py-1 file:px-3 file:rounded file:border-0 file:text-sm file:bg-gray-100 file:text-gray-700 hover:file:bg-gray-200"
                    />
                    {icon && (
                      <button
                        type="button"
                        onClick={() => setIcon(null)}
                        className="text-xs text-red-500 hover:text-red-700"
                      >
                        Remove
                      </button>
                    )}
                  </div>
                </div>
              </div>
              <div className="flex items-center gap-2 col-span-3">
                <input
                  id="tmpl-exclusive"
                  type="checkbox"
                  checked={exclusive}
                  onChange={(e) => setExclusive(e.target.checked)}
                  className="h-4 w-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
                />
                <label
                  htmlFor="tmpl-exclusive"
                  className="text-sm font-medium text-gray-700"
                >
                  Exclusive (single reservation at a time)
                </label>
              </div>
              {templateType === "device" && (
                <div>
                  <label
                    htmlFor="tmpl-driver"
                    className="block text-sm font-medium text-gray-700 mb-1"
                  >
                    Driver
                  </label>
                  <select
                    id="tmpl-driver"
                    value={driverId ?? ""}
                    onChange={(e) =>
                      setDriverId(e.target.value || null)
                    }
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                  >
                    <option value="">Select a driver</option>
                    {drivers?.map((d) => (
                      <option key={d.id} value={d.id}>
                        {d.name} ({d.connection_type})
                      </option>
                    ))}
                  </select>
                </div>
              )}
              <div>
                <label
                  htmlFor="tmpl-desc"
                  className="block text-sm font-medium text-gray-700 mb-1"
                >
                  Description
                </label>
                <textarea
                  id="tmpl-desc"
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  rows={2}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>
            </div>

            {templateType === "device" && (
              <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-3">
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-semibold text-gray-700">
                    Hardware Identity
                  </h3>
                  {aiStatus?.enabled && (
                    <button
                      type="button"
                      onClick={handleSuggestIdentity}
                      disabled={suggestIdentity.isPending}
                      className="text-xs font-medium text-blue-600 hover:text-blue-800 disabled:opacity-50"
                    >
                      {suggestIdentity.isPending ? "Suggesting..." : "Suggest with AI"}
                    </button>
                  )}
                </div>
                <p className="text-xs text-gray-500">
                  Vendor and Model are used by the AI assistant to ground
                  vendor-specific guidance. Both are required for device
                  templates.
                </p>
                <div className="grid grid-cols-3 gap-4">
                  <div>
                    <label
                      htmlFor="tmpl-vendor"
                      className="block text-sm font-medium text-gray-700 mb-1"
                    >
                      Vendor <span className="text-red-500">*</span>
                    </label>
                    <input
                      id="tmpl-vendor"
                      type="text"
                      value={vendor}
                      onChange={(e) => setVendor(e.target.value)}
                      placeholder="Cisco"
                      className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                    />
                  </div>
                  <div>
                    <label
                      htmlFor="tmpl-model"
                      className="block text-sm font-medium text-gray-700 mb-1"
                    >
                      Model <span className="text-red-500">*</span>
                    </label>
                    <input
                      id="tmpl-model"
                      type="text"
                      value={model}
                      onChange={(e) => setModel(e.target.value)}
                      placeholder="Catalyst 9300"
                      className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                    />
                  </div>
                  <div>
                    <label
                      htmlFor="tmpl-pn"
                      className="block text-sm font-medium text-gray-700 mb-1"
                    >
                      Part Number
                    </label>
                    <input
                      id="tmpl-pn"
                      type="text"
                      value={partNumber}
                      onChange={(e) => setPartNumber(e.target.value)}
                      placeholder="C9300-48UXM-A"
                      className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                    />
                  </div>
                  <div>
                    <label
                      htmlFor="tmpl-poll-interval"
                      className="block text-sm font-medium text-gray-700 mb-1"
                    >
                      Health-poll interval (seconds)
                    </label>
                    <input
                      id="tmpl-poll-interval"
                      type="number"
                      min={30}
                      step={1}
                      value={pollInterval}
                      onChange={(e) => setPollInterval(e.target.value)}
                      placeholder="leave blank for no polling"
                      className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                    />
                    <p className="text-xs text-gray-500 mt-1">
                      Default for devices created from this template. Minimum 30. Individual devices may override.
                    </p>
                  </div>
                </div>
                {!identityValid && (
                  <p className="text-xs text-red-500">
                    Vendor and Model are required for device templates.
                  </p>
                )}
              </div>
            )}

            {sections.map((section, si) => (
              <div
                key={si}
                className="bg-white rounded-lg border border-gray-200 p-4 space-y-3"
              >
                <div className="flex items-center justify-between">
                  <input
                    type="text"
                    placeholder="Section name"
                    value={section.name}
                    onChange={(e) => updateSection(si, { name: e.target.value })}
                    className="text-sm font-semibold border border-gray-300 rounded px-2 py-1 focus:outline-none focus:ring-2 focus:ring-blue-500"
                  />
                  <button
                    type="button"
                    onClick={() => removeSection(si)}
                    className="text-sm text-red-500 hover:text-red-700"
                  >
                    Remove Section
                  </button>
                </div>

                <div className="space-y-2">
                  {section.fields.map((field, fi) => (
                    <FieldRow
                      key={fi}
                      field={field}
                      index={fi}
                      onChange={(idx, f) => updateField(si, idx, f)}
                      onRemove={(idx) => removeField(si, idx)}
                    />
                  ))}
                </div>

                <button
                  type="button"
                  onClick={() => addField(si)}
                  className="text-sm text-blue-600 hover:text-blue-800"
                >
                  + Add Field
                </button>
              </div>
            ))}

            <button
              type="button"
              onClick={addSection}
              className="w-full py-2 text-sm font-medium text-blue-600 border border-dashed border-blue-300 rounded-lg hover:bg-blue-50 transition-colors"
            >
              + Add Section
            </button>
          </>
        ) : (
          <>
            <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-4">
              <div className="flex items-center gap-4">
                {existing?.icon ? (
                  <img
                    src={existing.icon}
                    alt={existing.name}
                    className="w-12 h-12 object-contain rounded border border-gray-200"
                  />
                ) : (
                  <span className="inline-block w-12 h-12 bg-gray-200 rounded" />
                )}
                <div>
                  <h3 className="text-base font-semibold text-gray-900">{existing?.name}</h3>
                  <p className="text-sm text-gray-500 capitalize">{existing?.template_type} template</p>
                </div>
              </div>
              <div className="grid grid-cols-2 gap-4 pt-2">
                <div>
                  <p className="text-xs font-medium text-gray-500 uppercase mb-1">Description</p>
                  <p className="text-sm text-gray-600">{existing?.description ?? "-"}</p>
                </div>
                <div>
                  <p className="text-xs font-medium text-gray-500 uppercase mb-1">Driver</p>
                  <p className="text-sm text-gray-600">
                    {existing?.driver_name
                      ? `${existing.driver_name} (${existing.connection_type})`
                      : "-"}
                  </p>
                </div>
                <div>
                  <p className="text-xs font-medium text-gray-500 uppercase mb-1">Vendor</p>
                  <p className="text-sm text-gray-600">
                    {existing && existing.vendor !== "unknown" ? existing.vendor : "-"}
                  </p>
                </div>
                <div>
                  <p className="text-xs font-medium text-gray-500 uppercase mb-1">Model</p>
                  <p className="text-sm text-gray-600">
                    {existing && existing.model !== "unknown" ? existing.model : "-"}
                  </p>
                </div>
                <div>
                  <p className="text-xs font-medium text-gray-500 uppercase mb-1">Part Number</p>
                  <p className="text-sm text-gray-600">{existing?.part_number ?? "-"}</p>
                </div>
                <div>
                  <p className="text-xs font-medium text-gray-500 uppercase mb-1">
                    Health-poll interval
                  </p>
                  <p className="text-sm text-gray-600">
                    {existing?.poll_interval_seconds !== null &&
                    existing?.poll_interval_seconds !== undefined
                      ? `${existing.poll_interval_seconds}s`
                      : "Not polled"}
                  </p>
                </div>
                <div>
                  <p className="text-xs font-medium text-gray-500 uppercase mb-1">ID</p>
                  <p className="text-sm text-gray-600 font-mono">{existing?.id}</p>
                </div>
                <div>
                  <p className="text-xs font-medium text-gray-500 uppercase mb-1">Updated</p>
                  <p className="text-sm text-gray-600">
                    {existing ? new Date(existing.updated_at).toLocaleDateString() : "-"}
                  </p>
                </div>
              </div>
            </div>

            {existing?.sections.map((section) => (
              <div
                key={section.name}
                className="bg-white rounded-lg border border-gray-200 p-4 space-y-3"
              >
                <h3 className="text-sm font-semibold text-gray-700">{section.name}</h3>
                {section.fields.length === 0 ? (
                  <p className="text-sm text-gray-400">No fields</p>
                ) : (
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-gray-200">
                        <th className="text-left text-xs font-medium text-gray-500 uppercase py-1 px-2">Key</th>
                        <th className="text-left text-xs font-medium text-gray-500 uppercase py-1 px-2">Label</th>
                        <th className="text-left text-xs font-medium text-gray-500 uppercase py-1 px-2">Type</th>
                        <th className="text-left text-xs font-medium text-gray-500 uppercase py-1 px-2">Required</th>
                        <th className="text-left text-xs font-medium text-gray-500 uppercase py-1 px-2">Default</th>
                        <th className="text-left text-xs font-medium text-gray-500 uppercase py-1 px-2">Options</th>
                      </tr>
                    </thead>
                    <tbody>
                      {section.fields.map((field) => (
                        <tr key={field.key} className="border-b border-gray-100">
                          <td className="py-1 px-2 font-mono text-gray-600">{field.key}</td>
                          <td className="py-1 px-2 text-gray-900">{field.label}</td>
                          <td className="py-1 px-2 text-gray-600">{FIELD_TYPE_LABELS[field.type] ?? field.type}</td>
                          <td className="py-1 px-2 text-gray-600">{field.required ? "Yes" : "No"}</td>
                          <td className="py-1 px-2 text-gray-600">{field.default != null ? String(field.default) : "-"}</td>
                          <td className="py-1 px-2 text-gray-600">{field.options?.join(", ") ?? "-"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  );
}
