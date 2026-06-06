import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeAll } from "vitest";
import { BulkImportExport } from "@/components/ui/BulkImportExport";
import type { BulkImportReport } from "@/api/bulk";

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = vi.fn();
  HTMLDialogElement.prototype.close = vi.fn();
});

const report: BulkImportReport = {
  dry_run: true,
  total: 2,
  created: 1,
  updated: 0,
  skipped: 0,
  rejected: 1,
  rows: [
    { row: 0, action: "create", identity: "FW-01", reason: null },
    { row: 1, action: "reject", identity: "BAD-01", reason: "template not found" },
  ],
};

describe("BulkImportExport", () => {
  it("renders export buttons and import trigger", () => {
    render(
      <BulkImportExport
        resourceLabel="devices"
        onExport={vi.fn()}
        onImport={vi.fn()}
      />,
    );
    expect(screen.getByText("Export JSON")).toBeInTheDocument();
    expect(screen.getByText("Export CSV")).toBeInTheDocument();
    expect(screen.getByText("Import")).toBeInTheDocument();
  });

  it("calls onExport with the chosen format", () => {
    const onExport = vi.fn().mockResolvedValue(undefined);
    render(
      <BulkImportExport
        resourceLabel="devices"
        onExport={onExport}
        onImport={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByText("Export CSV"));
    expect(onExport).toHaveBeenCalledWith("csv");
    fireEvent.click(screen.getByText("Export JSON"));
    expect(onExport).toHaveBeenCalledWith("json");
  });

  it("runs a dry-run and shows the per-row reject report", async () => {
    const onImport = vi.fn().mockResolvedValue(report);
    render(
      <BulkImportExport
        resourceLabel="devices"
        onExport={vi.fn()}
        onImport={onImport}
      />,
    );
    fireEvent.click(screen.getByText("Import"));
    const file = new File(['[]'], "devices.json", { type: "application/json" });
    const input = screen.getByLabelText("File") as HTMLInputElement;
    fireEvent.change(input, { target: { files: [file] } });

    fireEvent.click(screen.getByText("Dry run"));
    await waitFor(() => expect(onImport).toHaveBeenCalledWith(file, "json", true));
    await waitFor(() => expect(screen.getByText(/Dry-run preview/)).toBeInTheDocument());
    expect(screen.getByText(/BAD-01.*template not found/)).toBeInTheDocument();
  });

  it("infers csv format from the file extension", async () => {
    const onImport = vi.fn().mockResolvedValue({ ...report, dry_run: false, rejected: 0, rows: [] });
    const onImported = vi.fn();
    render(
      <BulkImportExport
        resourceLabel="devices"
        onExport={vi.fn()}
        onImport={onImport}
        onImported={onImported}
      />,
    );
    fireEvent.click(screen.getByText("Import"));
    const file = new File(['x'], "devices.csv", { type: "text/csv" });
    fireEvent.change(screen.getByLabelText("File"), { target: { files: [file] } });
    fireEvent.click(screen.getByText("Import now"));
    await waitFor(() => expect(onImport).toHaveBeenCalledWith(file, "csv", false));
    await waitFor(() => expect(onImported).toHaveBeenCalled());
  });
});
