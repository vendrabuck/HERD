// Bulk import/export API for devices, templates, and topologies.
//
// Export downloads a CSV or JSON file. Import uploads a file and returns a
// per-row report; with dryRun=true nothing is written and the report previews
// the outcome. Identity keys for cross-instance reference resolution: device by
// name (template ref by template name), template by name (driver ref by driver
// name), topology by name (canvas device refs by device name).

import apiClient from "./client";

export type BulkFormat = "csv" | "json";

export type RowAction = "create" | "update" | "skip" | "reject";

export interface BulkRowResult {
  row: number;
  action: RowAction;
  identity: string | null;
  reason: string | null;
}

export interface BulkImportReport {
  dry_run: boolean;
  total: number;
  created: number;
  updated: number;
  skipped: number;
  rejected: number;
  rows: BulkRowResult[];
}

// resource path under the gateway, e.g. "inventory/devices" or "cabling/topologies".
async function exportResource(resourcePath: string, format: BulkFormat): Promise<void> {
  const resp = await apiClient.get(`/${resourcePath}/export`, {
    params: { format },
    responseType: "blob",
  });
  const blob = new Blob([resp.data]);
  const url = window.URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  const name = resourcePath.split("/").pop() ?? "export";
  link.download = `${name}.${format}`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(url);
}

async function importResource(
  resourcePath: string,
  file: File,
  format: BulkFormat,
  dryRun: boolean,
): Promise<BulkImportReport> {
  const form = new FormData();
  form.append("file", file);
  const resp = await apiClient.post<BulkImportReport>(`/${resourcePath}/import`, form, {
    params: { format, dry_run: dryRun },
    headers: { "Content-Type": "multipart/form-data" },
  });
  return resp.data;
}

export const exportDevices = (format: BulkFormat) => exportResource("inventory/devices", format);
export const importDevices = (file: File, format: BulkFormat, dryRun: boolean) =>
  importResource("inventory/devices", file, format, dryRun);

export const exportTemplates = (format: BulkFormat) =>
  exportResource("inventory/templates", format);
export const importTemplates = (file: File, format: BulkFormat, dryRun: boolean) =>
  importResource("inventory/templates", file, format, dryRun);

export const exportTopologies = (format: BulkFormat) =>
  exportResource("cabling/topologies", format);
export const importTopologies = (file: File, format: BulkFormat, dryRun: boolean) =>
  importResource("cabling/topologies", file, format, dryRun);
