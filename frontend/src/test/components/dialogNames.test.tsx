import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi } from "vitest";
import { Modal } from "@/components/ui/Modal";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { BulkImportExport } from "@/components/ui/BulkImportExport";

// Issue #1318: every mounted dialog is named by its OWN title. Closed dialogs
// stay mounted, so a literal id shared by every instance would resolve each
// aria-labelledby to the first title in the document. These tests rely on the
// setup.ts showModal/close stubs, which toggle the open attribute.

// Body only: react-hot-toast's styling library injects <style id="_goober">
// tags into the head, which are not page content.
function allIds(): string[] {
  return [...document.body.querySelectorAll("[id]")].map((el) => el.id);
}

function expectUniqueIds() {
  const ids = allIds();
  const dupes = ids.filter((id, i) => ids.indexOf(id) !== i);
  expect(dupes).toEqual([]);
}

function expectReferencesInside(dialog: HTMLElement, attr: string) {
  const ref = dialog.getAttribute(attr);
  expect(ref, attr).toBeTruthy();
  const target = document.getElementById(ref as string);
  expect(target, `${attr} target`).not.toBe(null);
  expect(dialog.contains(target)).toBe(true);
}

describe("Modal accessible names with several mounted (issue #1318)", () => {
  function renderEditorLike() {
    // The topology editor shape: one closed Modal mounted first, then open ones.
    return render(
      <>
        <Modal open={false} onClose={vi.fn()} title="Generate topology with AI">
          ai
        </Modal>
        <Modal open onClose={vi.fn()} title="Save topology as template">
          save
        </Modal>
        <Modal open onClose={vi.fn()} title="Fork conflict">
          fork
        </Modal>
      </>,
    );
  }

  it("names each open dialog by its own title", () => {
    renderEditorLike();
    const save = screen.getByRole("dialog", { name: "Save topology as template" });
    expect(within(save).getByText("save")).toBeInTheDocument();
    const fork = screen.getByRole("dialog", { name: "Fork conflict" });
    expect(within(fork).getByText("fork")).toBeInTheDocument();
    // Only the two open dialogs are in the accessibility tree.
    expect(screen.getAllByRole("dialog")).toHaveLength(2);
  });

  it("puts no duplicate ids in the document and labels every dialog from inside itself", () => {
    renderEditorLike();
    expectUniqueIds();
    const dialogs = [...document.querySelectorAll("dialog")] as HTMLElement[];
    expect(dialogs).toHaveLength(3);
    for (const dialog of dialogs) expectReferencesInside(dialog, "aria-labelledby");
  });
});

describe("ConfirmDialog accessible names with several mounted (issue #1318)", () => {
  it("names and describes each dialog from inside itself, with no duplicate ids", () => {
    // ReservationsPage mounts one ConfirmDialog per row.
    render(
      <>
        <ConfirmDialog
          open={false}
          title="Cancel reservation A"
          description="Desc A"
          onConfirm={vi.fn()}
          onCancel={vi.fn()}
        />
        <ConfirmDialog
          open
          title="Cancel reservation B"
          description="Desc B"
          onConfirm={vi.fn()}
          onCancel={vi.fn()}
        />
      </>,
    );
    const open = screen.getByRole("dialog", { name: "Cancel reservation B" });
    expect(open).toHaveAccessibleDescription("Desc B");
    expectUniqueIds();
    for (const dialog of document.querySelectorAll("dialog")) {
      expectReferencesInside(dialog as HTMLElement, "aria-labelledby");
      expectReferencesInside(dialog as HTMLElement, "aria-describedby");
    }
  });
});

describe("BulkImportExport beside other dialogs (issue #1318)", () => {
  it("names the open import dialog by its own title, with no duplicate ids", async () => {
    // The TopologyPage shape: one BulkImportExport plus a Modal and a
    // ConfirmDialog. (Its file inputs keep literal ids that e2e tests select
    // by; no page mounts two BulkImportExports.)
    const user = userEvent.setup();
    const noop = vi.fn();
    render(
      <>
        <Modal open={false} onClose={vi.fn()} title="New Topology">
          new
        </Modal>
        <ConfirmDialog
          open={false}
          title="Delete topology"
          description="Sure?"
          onConfirm={vi.fn()}
          onCancel={vi.fn()}
        />
        <BulkImportExport resourceLabel="topologies" onExport={noop} onImport={noop} />
      </>,
    );
    await user.click(screen.getByRole("button", { name: "Import" }));
    expect(screen.getByRole("dialog", { name: "Import topologies" })).toBeInTheDocument();
    expectUniqueIds();
    for (const dialog of document.querySelectorAll("dialog")) {
      expectReferencesInside(dialog as HTMLElement, "aria-labelledby");
    }
  });
});
