import { vi, beforeEach, afterEach, describe, it, expect } from "vitest";

// The bulk client only re-exports thin wrappers over apiClient.get/post plus a
// DOM-driven download path. We mock apiClient directly so we can assert the
// exact URLs, params, headers, and FormData that each wrapper builds, and stub
// the jsdom-missing URL.createObjectURL / revokeObjectURL used by the download.
const get = vi.fn();
const post = vi.fn();

vi.mock("@/api/client", () => ({
  default: { get: (...a: unknown[]) => get(...a), post: (...a: unknown[]) => post(...a) },
}));

import {
  exportDevices,
  importDevices,
  exportTemplates,
  importTemplates,
  exportTopologies,
  importTopologies,
  type BulkImportReport,
} from "@/api/bulk";

const REPORT: BulkImportReport = {
  dry_run: true,
  total: 2,
  created: 1,
  updated: 0,
  skipped: 1,
  rejected: 0,
  rows: [
    { row: 1, action: "create", identity: "r1", reason: null },
    { row: 2, action: "skip", identity: "r2", reason: "exists" },
  ],
};

describe("bulk export", () => {
  let createdUrl: string;
  let revoked: string[];
  let clicked: number;
  let appended: HTMLElement[];

  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    createdUrl = "blob:mock-url";
    revoked = [];
    clicked = 0;
    appended = [];

    // jsdom does not implement the object-URL API the download path relies on.
    window.URL.createObjectURL = vi.fn(() => createdUrl);
    window.URL.revokeObjectURL = vi.fn((u: string) => {
      revoked.push(u);
    });

    vi.spyOn(document.body, "appendChild").mockImplementation((node) => {
      appended.push(node as HTMLElement);
      return node as HTMLElement;
    });
    // The created anchor's click() must be a no-op (jsdom would navigate).
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clicked++;
    });
    vi.spyOn(HTMLAnchorElement.prototype, "remove").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("exportDevices requests the devices export as a blob and downloads it", async () => {
    get.mockResolvedValue({ data: new Uint8Array([1, 2, 3]) });

    await exportDevices("csv");

    expect(get).toHaveBeenCalledWith("/inventory/devices/export", {
      params: { format: "csv" },
      responseType: "blob",
    });
    // The download wiring fired exactly once and cleaned up its object URL.
    expect(clicked).toBe(1);
    expect(appended).toHaveLength(1);
    const anchor = appended[0] as HTMLAnchorElement;
    expect(anchor.download).toBe("devices.csv");
    expect(anchor.href).toContain(createdUrl);
    expect(revoked).toEqual([createdUrl]);
  });

  it("exportTemplates names the file after the last path segment and format", async () => {
    get.mockResolvedValue({ data: new Uint8Array([9]) });

    await exportTemplates("json");

    expect(get).toHaveBeenCalledWith("/inventory/templates/export", {
      params: { format: "json" },
      responseType: "blob",
    });
    expect((appended[0] as HTMLAnchorElement).download).toBe("templates.json");
  });

  it("exportTopologies targets the cabling resource path", async () => {
    get.mockResolvedValue({ data: new Uint8Array([7]) });

    await exportTopologies("json");

    expect(get).toHaveBeenCalledWith("/cabling/topologies/export", {
      params: { format: "json" },
      responseType: "blob",
    });
    expect((appended[0] as HTMLAnchorElement).download).toBe("topologies.json");
  });
});

describe("bulk import", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
  });

  it("importDevices posts multipart form data with format and dry_run params", async () => {
    post.mockResolvedValue({ data: REPORT });
    const file = new File(["name\n"], "devices.csv", { type: "text/csv" });

    const report = await importDevices(file, "csv", true);

    expect(report).toEqual(REPORT);
    expect(post).toHaveBeenCalledTimes(1);
    const [url, form, opts] = post.mock.calls[0];
    expect(url).toBe("/inventory/devices/import");
    expect(form).toBeInstanceOf(FormData);
    expect((form as FormData).get("file")).toBe(file);
    expect(opts).toEqual({
      params: { format: "csv", dry_run: true },
      headers: { "Content-Type": "multipart/form-data" },
    });
  });

  it("importTemplates threads dry_run=false through to the params", async () => {
    post.mockResolvedValue({ data: { ...REPORT, dry_run: false } });
    const file = new File(["x"], "t.json", { type: "application/json" });

    const report = await importTemplates(file, "json", false);

    expect(report.dry_run).toBe(false);
    const [url, , opts] = post.mock.calls[0];
    expect(url).toBe("/inventory/templates/import");
    expect(opts.params).toEqual({ format: "json", dry_run: false });
  });

  it("importTopologies targets the cabling import path", async () => {
    post.mockResolvedValue({ data: REPORT });
    const file = new File(["x"], "topo.json", { type: "application/json" });

    await importTopologies(file, "json", true);

    expect(post.mock.calls[0][0]).toBe("/cabling/topologies/import");
  });

  it("propagates a server error from import", async () => {
    post.mockRejectedValue(new Error("422 unprocessable"));
    const file = new File(["bad"], "d.csv", { type: "text/csv" });

    await expect(importDevices(file, "csv", false)).rejects.toThrow("422 unprocessable");
  });
});
