import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeAll } from "vitest";
import { Modal } from "@/components/ui/Modal";

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = vi.fn();
  HTMLDialogElement.prototype.close = vi.fn();
});

describe("Modal", () => {
  it("renders title and children when open", () => {
    render(
      <Modal open={true} onClose={vi.fn()} title="Test Title">
        <p>Modal content</p>
      </Modal>
    );
    expect(screen.getByText("Test Title")).toBeInTheDocument();
    expect(screen.getByText("Modal content")).toBeInTheDocument();
  });

  it("calls showModal when open", () => {
    const showModalSpy = vi.fn();
    HTMLDialogElement.prototype.showModal = showModalSpy;
    render(
      <Modal open={true} onClose={vi.fn()} title="T">
        content
      </Modal>
    );
    expect(showModalSpy).toHaveBeenCalled();
  });

  it("close button calls onClose", () => {
    const onClose = vi.fn();
    render(
      <Modal open={true} onClose={onClose} title="T">
        content
      </Modal>
    );
    fireEvent.click(screen.getByLabelText("Close dialog"));
    expect(onClose).toHaveBeenCalled();
  });

  it("does not call showModal when closed", () => {
    const showModalSpy = vi.fn();
    HTMLDialogElement.prototype.showModal = showModalSpy;
    showModalSpy.mockClear();
    render(
      <Modal open={false} onClose={vi.fn()} title="T">
        content
      </Modal>
    );
    expect(showModalSpy).not.toHaveBeenCalled();
  });
});
