import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeAll, afterEach } from "vitest";

const { toastError } = vi.hoisted(() => ({ toastError: vi.fn() }));
vi.mock("react-hot-toast", () => ({
  default: Object.assign(vi.fn(), { error: toastError, success: vi.fn() }),
}));

import { BulkImportExport } from "@/components/ui/BulkImportExport";
import type { BulkFormat, BulkImportReport } from "@/api/bulk";

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = vi.fn();
  HTMLDialogElement.prototype.close = vi.fn();
});

afterEach(() => {
  toastError.mockClear();
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

// Issue #1575: a refused import says why, inside the dialog. A 413 or 422 is
// the server refusing the file itself, in fixed copy written for the user, so
// its detail is shown through the shared errorDetail; every other failure is
// the fixed sentence, whatever its body holds.
describe("BulkImportExport refused import (issue #1575)", () => {
  function refusal(status: number | undefined, body?: unknown) {
    const err = new Error("Request failed") as Error & { response?: unknown };
    if (status !== undefined) err.response = { status, data: body };
    return err;
  }

  function openWithFile(
    onImport: (file: File, format: BulkFormat, dryRun: boolean) => Promise<BulkImportReport>,
    resourceLabel = "devices",
  ) {
    render(
      <BulkImportExport resourceLabel={resourceLabel} onExport={vi.fn()} onImport={onImport} />,
    );
    fireEvent.click(screen.getByText("Import"));
    const file = new File(["["], `${resourceLabel}.json`, { type: "application/json" });
    fireEvent.change(screen.getByLabelText("File"), { target: { files: [file] } });
  }

  // The dialog's showModal is stubbed to a no-op in this file, so it is not
  // "open" and its contents are hidden from role queries without `hidden`.
  const alertText = async () =>
    (await screen.findByRole("alert", { hidden: true })).textContent;

  it.each([
    [
      "a 422 invalid JSON detail",
      refusal(422, { detail: "Invalid JSON: Expecting value: line 1 column 2 (char 1)" }),
      "Invalid JSON: Expecting value: line 1 column 2 (char 1)",
    ],
    [
      "a 422 encoding detail",
      refusal(422, { detail: "Import file must be UTF-8 encoded; re-save it as UTF-8 and retry" }),
      "Import file must be UTF-8 encoded; re-save it as UTF-8 and retry",
    ],
    [
      "the 413 body limit item",
      refusal(413, {
        detail: [
          {
            type: "body_too_large",
            loc: ["body"],
            msg: "Request body is larger than 10485760 bytes",
          },
        ],
      }),
      "body: Request body is larger than 10485760 bytes",
    ],
    ["a 422 with an object detail", refusal(422, { detail: { error: "x" } }), "Failed to import devices"],
    ["a 422 with a blank detail", refusal(422, { detail: "  " }), "Failed to import devices"],
    ["a 422 with no body", refusal(422), "Failed to import devices"],
    ["a 422 whose body is an HTML page", refusal(422, "<html>422</html>"), "Failed to import devices"],
    ["a 500 with no detail", refusal(500, "Internal Server Error"), "Failed to import devices"],
    [
      "a 503 with a string detail",
      refusal(503, { detail: "could not resolve device names via inventory: timeout" }),
      "Failed to import devices",
    ],
    [
      "a 400 with a string detail",
      refusal(400, { detail: "Bad request" }),
      "Failed to import devices",
    ],
    [
      "a 409 with a string detail",
      refusal(409, { detail: "Conflict" }),
      "Failed to import devices",
    ],
    [
      "a 403 with a string detail",
      refusal(403, { detail: "Admin or superadmin role required" }),
      "Failed to import devices",
    ],
    ["a transport failure with no response", refusal(undefined), "Failed to import devices"],
  ])("%s shows exactly its text in the dialog", async (_name, err, expected) => {
    const onImport = vi.fn().mockRejectedValue(err);
    openWithFile(onImport);
    fireEvent.click(screen.getByText("Dry run"));

    expect(await alertText()).toBe(expected);
    expect(toastError).not.toHaveBeenCalled();
  });

  it("names the resource in the fallback (topologies)", async () => {
    openWithFile(vi.fn().mockRejectedValue(refusal(500)), "topologies");
    fireEvent.click(screen.getByText("Import now"));
    expect(await alertText()).toBe("Failed to import topologies");
  });

  it("renders markup in a detail as text, never as HTML", async () => {
    const markup = 'Invalid JSON: <img src="x" alt="injected"><b>bold</b>';
    openWithFile(vi.fn().mockRejectedValue(refusal(422, { detail: markup })));
    fireEvent.click(screen.getByText("Dry run"));

    expect(await alertText()).toBe(markup);
    expect(document.querySelector("img[alt='injected']")).toBeNull();
    expect(document.querySelector("dialog b")).toBeNull();
  });

  it("clears the reason when another file is chosen", async () => {
    openWithFile(vi.fn().mockRejectedValue(refusal(422, { detail: "Invalid JSON: x" })));
    fireEvent.click(screen.getByText("Dry run"));
    expect(await alertText()).toBe("Invalid JSON: x");

    const fixed = new File(["[]"], "fixed.json", { type: "application/json" });
    fireEvent.change(screen.getByLabelText("File"), { target: { files: [fixed] } });
    expect(screen.queryByRole("alert", { hidden: true })).not.toBeInTheDocument();
  });

  it("clears the reason when a later run succeeds", async () => {
    const onImport = vi
      .fn()
      .mockRejectedValueOnce(refusal(422, { detail: "Invalid JSON: x" }))
      .mockResolvedValueOnce(report);
    openWithFile(onImport);
    fireEvent.click(screen.getByText("Dry run"));
    expect(await alertText()).toBe("Invalid JSON: x");

    fireEvent.click(screen.getByText("Dry run"));
    await waitFor(() => expect(screen.getByText(/Dry-run preview/)).toBeInTheDocument());
    expect(screen.queryByRole("alert", { hidden: true })).not.toBeInTheDocument();
  });

  it("clears the reason when the dialog is closed", async () => {
    openWithFile(vi.fn().mockRejectedValue(refusal(422, { detail: "Invalid JSON: x" })));
    fireEvent.click(screen.getByText("Dry run"));
    expect(await alertText()).toBe("Invalid JSON: x");

    fireEvent.click(screen.getByText("Close"));
    expect(screen.queryByRole("alert", { hidden: true })).not.toBeInTheDocument();
  });
});
