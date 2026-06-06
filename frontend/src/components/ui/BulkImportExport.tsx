import { useEffect, useRef, useState } from "react";
import toast from "react-hot-toast";
import type { BulkFormat, BulkImportReport } from "@/api/bulk";

interface BulkImportExportProps {
  // Human label for the resource, e.g. "devices", "templates", "topologies".
  resourceLabel: string;
  onExport: (format: BulkFormat) => Promise<void>;
  onImport: (file: File, format: BulkFormat, dryRun: boolean) => Promise<BulkImportReport>;
  // Called after a committed (non-dry-run) import so the caller can refetch.
  onImported?: () => void;
}

// Infer the file format from its extension; falls back to json.
function inferFormat(name: string): BulkFormat {
  return name.toLowerCase().endsWith(".csv") ? "csv" : "json";
}

export function BulkImportExport({
  resourceLabel,
  onExport,
  onImport,
  onImported,
}: BulkImportExportProps) {
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [format, setFormat] = useState<BulkFormat>("json");
  const [report, setReport] = useState<BulkImportReport | null>(null);
  const [busy, setBusy] = useState(false);
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    else if (!open && dialog.open) dialog.close();
  }, [open]);

  const reset = () => {
    setFile(null);
    setReport(null);
    setBusy(false);
  };

  const close = () => {
    setOpen(false);
    reset();
  };

  const handleExport = async (fmt: BulkFormat) => {
    try {
      await onExport(fmt);
    } catch {
      toast.error(`Failed to export ${resourceLabel}`);
    }
  };

  const handleFile = (selected: File | null) => {
    setFile(selected);
    setReport(null);
    if (selected) setFormat(inferFormat(selected.name));
  };

  const runImport = async (dryRun: boolean) => {
    if (!file) return;
    setBusy(true);
    try {
      const result = await onImport(file, format, dryRun);
      setReport(result);
      if (!dryRun) {
        toast.success(
          `Imported ${resourceLabel}: ${result.created} created, ${result.updated} updated, ` +
            `${result.rejected} rejected`,
        );
        onImported?.();
      }
    } catch {
      toast.error(`Failed to import ${resourceLabel}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="flex items-center gap-2">
        <button
          onClick={() => handleExport("json")}
          className="px-3 py-1.5 text-sm font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors"
        >
          Export JSON
        </button>
        <button
          onClick={() => handleExport("csv")}
          className="px-3 py-1.5 text-sm font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors"
        >
          Export CSV
        </button>
        <button
          onClick={() => setOpen(true)}
          className="px-3 py-1.5 text-sm font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-lg transition-colors"
        >
          Import
        </button>
      </div>

      <dialog
        ref={dialogRef}
        className="rounded-lg shadow-xl border border-gray-200 p-0 backdrop:bg-black/40 max-w-2xl w-full"
        aria-labelledby="bulk-import-title"
        onClose={close}
      >
        <div className="p-6">
          <h2 id="bulk-import-title" className="text-lg font-semibold text-gray-900 mb-1">
            Import {resourceLabel}
          </h2>
          <p className="text-sm text-gray-600 mb-4">
            Upload a CSV or JSON file. Run a dry-run first to preview what would be created,
            updated, or rejected; nothing is written until you commit. References resolve by name,
            so an export from one HERD instance imports into another.
          </p>

          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1" htmlFor="bulk-file">
                File
              </label>
              <input
                id="bulk-file"
                type="file"
                accept=".csv,.json,application/json,text/csv"
                onChange={(e) => handleFile(e.target.files?.[0] ?? null)}
                className="block w-full text-sm text-gray-700"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1" htmlFor="bulk-format">
                Format
              </label>
              <select
                id="bulk-format"
                value={format}
                onChange={(e) => setFormat(e.target.value as BulkFormat)}
                className="block w-40 px-3 py-2 text-sm border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
              >
                <option value="json">JSON</option>
                <option value="csv">CSV</option>
              </select>
            </div>

            {report && (
              <div className="border border-gray-200 rounded-lg p-3" role="status" aria-live="polite">
                <p className="text-sm font-medium text-gray-900 mb-2">
                  {report.dry_run ? "Dry-run preview" : "Import result"}: {report.total} row(s),{" "}
                  {report.created} create, {report.updated} update, {report.skipped} skip,{" "}
                  {report.rejected} reject
                </p>
                {report.rejected > 0 && (
                  <ul className="text-xs text-red-600 max-h-40 overflow-y-auto space-y-1">
                    {report.rows
                      .filter((r) => r.action === "reject")
                      .map((r) => (
                        <li key={r.row}>
                          Row {r.row + 1}
                          {r.identity ? ` (${r.identity})` : ""}: {r.reason}
                        </li>
                      ))}
                  </ul>
                )}
              </div>
            )}
          </div>

          <div className="flex justify-end gap-3 mt-6">
            <button
              onClick={close}
              className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 rounded-lg hover:bg-gray-200 transition-colors"
            >
              Close
            </button>
            <button
              onClick={() => runImport(true)}
              disabled={!file || busy}
              className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 rounded-lg hover:bg-gray-200 transition-colors disabled:opacity-50"
            >
              Dry run
            </button>
            <button
              onClick={() => runImport(false)}
              disabled={!file || busy}
              className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors disabled:opacity-50"
            >
              Import now
            </button>
          </div>
        </div>
      </dialog>
    </>
  );
}
