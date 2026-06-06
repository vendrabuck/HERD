import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeAll } from "vitest";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = vi.fn();
  HTMLDialogElement.prototype.close = vi.fn();
});

describe("ConfirmDialog", () => {
  it("renders title and description", () => {
    render(
      <ConfirmDialog
        open={true}
        title="Delete Item"
        description="Are you sure?"
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />
    );
    expect(screen.getByText("Delete Item")).toBeInTheDocument();
    expect(screen.getByText("Are you sure?")).toBeInTheDocument();
  });

  it("confirm button calls onConfirm", () => {
    const onConfirm = vi.fn();
    render(
      <ConfirmDialog
        open={true}
        title="T"
        description="D"
        onConfirm={onConfirm}
        onCancel={vi.fn()}
      />
    );
    fireEvent.click(screen.getByText("Confirm"));
    expect(onConfirm).toHaveBeenCalled();
  });

  it("cancel button calls onCancel", () => {
    const onCancel = vi.fn();
    render(
      <ConfirmDialog
        open={true}
        title="T"
        description="D"
        onConfirm={vi.fn()}
        onCancel={onCancel}
      />
    );
    fireEvent.click(screen.getByText("Cancel"));
    expect(onCancel).toHaveBeenCalled();
  });

  it("destructive mode applies red styling", () => {
    render(
      <ConfirmDialog
        open={true}
        title="T"
        description="D"
        destructive={true}
        confirmLabel="Delete"
        onConfirm={vi.fn()}
        onCancel={vi.fn()}
      />
    );
    const btn = screen.getByText("Delete");
    expect(btn.className).toContain("bg-red-600");
  });
});
