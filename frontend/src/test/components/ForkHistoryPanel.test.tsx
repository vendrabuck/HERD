import { render, screen, fireEvent, within } from "@testing-library/react";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

const { toastError } = vi.hoisted(() => ({ toastError: vi.fn() }));
vi.mock("react-hot-toast", () => ({ default: { error: toastError, success: vi.fn() } }));

import { ForkHistoryPanel } from "@/components/topology-editor/ForkHistoryPanel";
import type { UseForkVersionPreviewResult } from "@/hooks/useForkVersionPreview";
import type { ForkCanvasDiff } from "@/lib/forkDiff";
import type { ForkVersionSummary } from "@/types/reservation.types";

// The <dialog>-based ConfirmDialog is opened by these showModal/close stubs
// (same pattern as ForkConflictDialog.test.tsx).
beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function showModal() {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function close() {
    this.removeAttribute("open");
  };
});

const V1: ForkVersionSummary = {
  id: "v1",
  fork_id: "f1",
  version_number: 1,
  restored_from_id: null,
  created_at: "2026-08-01T00:00:00Z",
};
const V2_RESTORED: ForkVersionSummary = {
  id: "v2",
  fork_id: "f1",
  version_number: 2,
  restored_from_id: "v1",
  created_at: "2026-08-02T00:00:00Z",
};

function makePreview(overrides: Partial<UseForkVersionPreviewResult> = {}): UseForkVersionPreviewResult {
  return {
    mode: "idle",
    isActive: false,
    previewVersion: null,
    previewLoading: false,
    startPreview: vi.fn(),
    diffBase: null,
    diffCompareLabel: null,
    diffLoading: false,
    diffResult: null,
    startDiff: vi.fn(),
    exit: vi.fn(),
    restoreVersion: vi.fn().mockResolvedValue(undefined),
    isRestoring: false,
    ...overrides,
  };
}

beforeEach(() => {
  toastError.mockClear();
});

describe("ForkHistoryPanel", () => {
  it("shows the restored marker on a version carrying restored_from_id", () => {
    render(
      <ForkHistoryPanel
        versions={[V1, V2_RESTORED]}
        isActiveReservation={false}
        draftRestoredFromId={null}
        preview={makePreview()}
        onClose={vi.fn()}
      />,
    );
    // v1 has no marker; only v2 (restored_from_id set) does.
    const restoreMarkers = screen.getAllByText("restore");
    expect(restoreMarkers).toHaveLength(1);
  });

  it("shows a draft-restored chip derived from draft_restored_from_id, not a new version row", () => {
    // Contract (revised 2026-08-28): restore appends no fork_versions row, so
    // the version list is unchanged; only the fork's draft_restored_from_id
    // (echoed here as the prop) signals the pending, unsaved restore.
    render(
      <ForkHistoryPanel
        versions={[V1, V2_RESTORED]}
        isActiveReservation={false}
        draftRestoredFromId={V1.id}
        preview={makePreview()}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByText("Draft restored from version 1 (unsaved)")).toBeInTheDocument();
    // Still exactly two version rows (both from fixtures, none synthesized).
    expect(screen.getByText("v1")).toBeInTheDocument();
    expect(screen.getByText("v2")).toBeInTheDocument();
  });

  it("shows no draft-restored chip when draft_restored_from_id is null", () => {
    render(
      <ForkHistoryPanel
        versions={[V1]}
        isActiveReservation={false}
        draftRestoredFromId={null}
        preview={makePreview()}
        onClose={vi.fn()}
      />,
    );
    expect(screen.queryByText(/Draft restored from/)).not.toBeInTheDocument();
  });

  it("renders Restore only for an ACTIVE reservation", () => {
    const { rerender } = render(
      <ForkHistoryPanel
        versions={[V1]}
        isActiveReservation={false}
        draftRestoredFromId={null}
        preview={makePreview()}
        onClose={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: "Restore" })).not.toBeInTheDocument();

    rerender(
      <ForkHistoryPanel
        versions={[V1]}
        isActiveReservation
        draftRestoredFromId={null}
        preview={makePreview()}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Restore" })).toBeInTheDocument();
  });

  it("Preview calls preview.startPreview with the clicked version", () => {
    const preview = makePreview();
    render(
      <ForkHistoryPanel
        versions={[V1]}
        isActiveReservation={false}
        draftRestoredFromId={null}
        preview={preview}
        onClose={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    expect(preview.startPreview).toHaveBeenCalledWith(V1);
  });

  it("Diff opens a compare-target picker and calls startDiff with the chosen target", () => {
    const preview = makePreview();
    render(
      <ForkHistoryPanel
        versions={[V1, V2_RESTORED]}
        isActiveReservation={false}
        draftRestoredFromId={null}
        preview={preview}
        onClose={vi.fn()}
      />,
    );
    fireEvent.click(screen.getAllByRole("button", { name: "Diff" })[0]);
    // Default target is "current draft"; Compare fires startDiff against it.
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    expect(preview.startDiff).toHaveBeenCalledWith(V1, { kind: "current" });
  });

  it("Diff against a specific version passes that version as the compare target", () => {
    const preview = makePreview();
    render(
      <ForkHistoryPanel
        versions={[V1, V2_RESTORED]}
        isActiveReservation={false}
        draftRestoredFromId={null}
        preview={preview}
        onClose={vi.fn()}
      />,
    );
    fireEvent.click(screen.getAllByRole("button", { name: "Diff" })[0]);
    fireEvent.change(screen.getByLabelText("Diff v1 against"), { target: { value: V2_RESTORED.id } });
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    expect(preview.startDiff).toHaveBeenCalledWith(V1, { kind: "version", version: V2_RESTORED });
  });

  it("renders the added/removed lists from a resolved diff result", () => {
    const diffResult: ForkCanvasDiff = {
      addedNodes: [
        {
          id: "n2",
          type: "deviceNode",
          position: { x: 0, y: 0 },
          data: { device: { id: "d2", name: "leaf-2" }, label: "leaf-2", topologyType: "PHYSICAL" },
        },
      ] as unknown as ForkCanvasDiff["addedNodes"],
      removedNodes: [],
      addedEdges: [
        {
          id: "e1",
          source: "n1",
          target: "n2",
          data: { layer: "L1", source_port_name: "et1", target_port_name: "et2" },
        },
      ] as unknown as ForkCanvasDiff["addedEdges"],
      removedEdges: [],
    };
    const preview = makePreview({
      mode: "diff",
      diffBase: V1,
      diffCompareLabel: "current draft",
      diffResult,
    });
    render(
      <ForkHistoryPanel
        versions={[V1]}
        isActiveReservation={false}
        draftRestoredFromId={null}
        preview={preview}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByText("Added devices (1)")).toBeInTheDocument();
    expect(screen.getByText("leaf-2")).toBeInTheDocument();
    expect(screen.getByText("Added connections (1)")).toBeInTheDocument();
    expect(screen.getByText("L1: et1 - et2")).toBeInTheDocument();
    expect(screen.queryByText(/Removed devices/)).not.toBeInTheDocument();
  });

  it("Restore opens a confirm dialog and only calls restoreVersion after confirming", () => {
    const preview = makePreview();
    render(
      <ForkHistoryPanel
        versions={[V1]}
        isActiveReservation
        draftRestoredFromId={null}
        preview={preview}
        onClose={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    expect(preview.restoreVersion).not.toHaveBeenCalled();
    expect(screen.getByText(/Nothing is wired until you run Save/)).toBeInTheDocument();

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Restore v1?")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Restore" }));
    expect(preview.restoreVersion).toHaveBeenCalledWith(V1);
  });

  it("Restore cancel does not call restoreVersion", () => {
    const preview = makePreview();
    render(
      <ForkHistoryPanel
        versions={[V1]}
        isActiveReservation
        draftRestoredFromId={null}
        preview={preview}
        onClose={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(preview.restoreVersion).not.toHaveBeenCalled();
  });

  it("Compare against a picked version that has since disappeared from versions shows an error instead of throwing", () => {
    const preview = makePreview();
    const { rerender } = render(
      <ForkHistoryPanel
        versions={[V1, V2_RESTORED]}
        isActiveReservation={false}
        draftRestoredFromId={null}
        preview={preview}
        onClose={vi.fn()}
      />,
    );
    fireEvent.click(screen.getAllByRole("button", { name: "Diff" })[0]);
    fireEvent.change(screen.getByLabelText("Diff v1 against"), { target: { value: V2_RESTORED.id } });

    // The picked target (v2) drops out of the versions list before Compare
    // is clicked, e.g. a stale render racing a fresh versions prop.
    rerender(
      <ForkHistoryPanel
        versions={[V1]}
        isActiveReservation={false}
        draftRestoredFromId={null}
        preview={preview}
        onClose={vi.fn()}
      />,
    );

    expect(() => fireEvent.click(screen.getByRole("button", { name: "Compare" }))).not.toThrow();
    expect(toastError).toHaveBeenCalledWith("That version is no longer available");
    expect(preview.startDiff).not.toHaveBeenCalled();
  });
});
