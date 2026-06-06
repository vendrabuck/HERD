import type { SectionDefinition } from "@/types/template.types";

interface DynamicFieldRendererProps {
  sections: SectionDefinition[];
  fieldData: Record<string, unknown>;
  onChange?: (fieldData: Record<string, unknown>) => void;
  readOnly?: boolean;
}

export function DynamicFieldRenderer({
  sections,
  fieldData,
  onChange,
  readOnly,
}: DynamicFieldRendererProps) {
  const update = (key: string, value: unknown) => {
    onChange?.({ ...fieldData, [key]: value });
  };

  return (
    <div className="space-y-4">
      {sections.map((section) => (
        <fieldset key={section.name} className="space-y-2">
          <legend className="text-sm font-semibold text-gray-700">
            {section.name}
          </legend>
          {section.fields.map((field) => {
            const value = fieldData[field.key] ?? field.default ?? "";
            return (
              <div key={field.key}>
                <label
                  htmlFor={`field-${field.key}`}
                  className="block text-sm text-gray-600 mb-0.5"
                >
                  {field.label}
                  {field.required && (
                    <span className="text-red-500 ml-0.5">*</span>
                  )}
                </label>

                {field.type === "string" && (
                  <input
                    id={`field-${field.key}`}
                    type="text"
                    value={String(value ?? "")}
                    onChange={(e) => update(field.key, e.target.value)}
                    required={field.required}
                    disabled={readOnly}
                    className="w-full border border-gray-300 rounded px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-50 disabled:text-gray-500"
                  />
                )}

                {field.type === "password" && readOnly && (
                  <span
                    id={`field-${field.key}`}
                    className="block w-full border border-gray-300 rounded px-2 py-1 text-sm bg-gray-50 text-gray-500"
                  >
                    ********
                  </span>
                )}

                {field.type === "password" && !readOnly && (
                  <input
                    id={`field-${field.key}`}
                    type="password"
                    value={String(value ?? "")}
                    onChange={(e) => update(field.key, e.target.value)}
                    placeholder="password"
                    required={field.required}
                    className="w-full border border-gray-300 rounded px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                  />
                )}

                {field.type === "number" && (
                  <input
                    id={`field-${field.key}`}
                    type="number"
                    value={value === "" ? "" : Number(value)}
                    onChange={(e) =>
                      update(
                        field.key,
                        e.target.value === "" ? "" : Number(e.target.value),
                      )
                    }
                    required={field.required}
                    disabled={readOnly}
                    className="w-full border border-gray-300 rounded px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-50 disabled:text-gray-500"
                  />
                )}

                {field.type === "boolean" && (
                  <input
                    id={`field-${field.key}`}
                    type="checkbox"
                    checked={Boolean(value)}
                    onChange={(e) => update(field.key, e.target.checked)}
                    disabled={readOnly}
                    className="rounded"
                  />
                )}

                {field.type === "dropdown" && (
                  <select
                    id={`field-${field.key}`}
                    value={String(value ?? "")}
                    onChange={(e) => update(field.key, e.target.value)}
                    required={field.required}
                    disabled={readOnly}
                    className="w-full border border-gray-300 rounded px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-50 disabled:text-gray-500"
                  >
                    <option value="">Select...</option>
                    {field.options?.map((opt) => (
                      <option key={opt} value={opt}>
                        {opt}
                      </option>
                    ))}
                  </select>
                )}
              </div>
            );
          })}
        </fieldset>
      ))}
    </div>
  );
}
