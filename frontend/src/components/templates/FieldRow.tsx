import { useEffect, useState } from "react";
import type { FieldDefinition, FieldType } from "@/types/template.types";

const FIELD_TYPES: { label: string; value: FieldType }[] = [
  { label: "String", value: "string" },
  { label: "Number", value: "number" },
  { label: "Boolean", value: "boolean" },
  { label: "Dropdown", value: "dropdown" },
  { label: "Password", value: "password" },
];

interface FieldRowProps {
  field: FieldDefinition;
  index: number;
  onChange: (index: number, field: FieldDefinition) => void;
  onRemove: (index: number) => void;
}

export function FieldRow({ field, index, onChange, onRemove }: FieldRowProps) {
  const [optionsText, setOptionsText] = useState(
    field.options?.join(", ") ?? ""
  );

  useEffect(() => {
    // Intentional state sync: re-seed local edit buffer from props when the
    // field type changes (e.g. switching to/from dropdown), not on every
    // field.options change which would overwrite mid-typing.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setOptionsText(field.options?.join(", ") ?? "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [field.type]);

  const update = (patch: Partial<FieldDefinition>) => {
    const updated = { ...field, ...patch };
    // Clear options and default when switching type
    if (patch.type && patch.type !== field.type) {
      updated.default = undefined;
      if (patch.type !== "dropdown") {
        updated.options = undefined;
      }
    }
    // Initialize options when switching to dropdown
    if (patch.type === "dropdown" && !updated.options) {
      updated.options = [];
    }
    onChange(index, updated);
  };

  return (
    <div className="flex items-start gap-2 p-2 bg-gray-50 rounded border border-gray-200">
      <div className="flex-1 grid grid-cols-2 gap-2">
        <input
          type="text"
          placeholder="Key"
          value={field.key}
          onChange={(e) => update({ key: e.target.value })}
          className="text-sm border border-gray-300 rounded px-2 py-1 focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
        <input
          type="text"
          placeholder="Label"
          value={field.label}
          onChange={(e) => update({ label: e.target.value })}
          className="text-sm border border-gray-300 rounded px-2 py-1 focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
        <select
          value={field.type}
          onChange={(e) => update({ type: e.target.value as FieldType })}
          className="text-sm border border-gray-300 rounded px-2 py-1 focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          {FIELD_TYPES.map((t) => (
            <option key={t.value} value={t.value}>
              {t.label}
            </option>
          ))}
        </select>
        <label className="flex items-center gap-1.5 text-sm text-gray-700">
          <input
            type="checkbox"
            checked={field.required ?? false}
            onChange={(e) => update({ required: e.target.checked })}
            className="rounded"
          />
          Required
        </label>
        {field.type === "dropdown" && (
          <div className="col-span-2">
            <input
              type="text"
              placeholder="Options (comma-separated)"
              value={optionsText}
              onChange={(e) => setOptionsText(e.target.value)}
              onBlur={() => {
                const parsed = optionsText
                  .split(",")
                  .map((s) => s.trim())
                  .filter(Boolean);
                update({ options: parsed });
              }}
              className="w-full text-sm border border-gray-300 rounded px-2 py-1 focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
        )}
        <div className="col-span-2 flex items-center gap-2">
          <label className="text-sm text-gray-600 whitespace-nowrap">Default:</label>
          {field.type === "boolean" ? (
            <label className="flex items-center gap-1.5 text-sm text-gray-700">
              <input
                type="checkbox"
                checked={(field.default as boolean) ?? false}
                onChange={(e) => update({ default: e.target.checked })}
                className="rounded"
              />
              {field.default ? "true" : "false"}
            </label>
          ) : field.type === "dropdown" ? (
            <select
              value={(field.default as string) ?? ""}
              onChange={(e) =>
                update({ default: e.target.value || undefined })
              }
              className="flex-1 text-sm border border-gray-300 rounded px-2 py-1 focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="">No default</option>
              {field.options?.map((opt) => (
                <option key={opt} value={opt}>
                  {opt}
                </option>
              ))}
            </select>
          ) : field.type === "number" ? (
            <input
              type="number"
              placeholder="No default"
              value={field.default != null ? String(field.default) : ""}
              onChange={(e) => {
                const num = Number(e.target.value);
                update({
                  default:
                    e.target.value === "" || Number.isNaN(num)
                      ? undefined
                      : num,
                });
              }}
              className="flex-1 text-sm border border-gray-300 rounded px-2 py-1 focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          ) : (
            <input
              type="text"
              placeholder="No default"
              value={(field.default as string) ?? ""}
              onChange={(e) =>
                update({
                  default: e.target.value || undefined,
                })
              }
              className="flex-1 text-sm border border-gray-300 rounded px-2 py-1 focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          )}
        </div>
      </div>
      <button
        type="button"
        onClick={() => onRemove(index)}
        className="text-sm text-red-500 hover:text-red-700 px-1 py-1 shrink-0"
        title="Remove field"
      >
        Remove
      </button>
    </div>
  );
}
