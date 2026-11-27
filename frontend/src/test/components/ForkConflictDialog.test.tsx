import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";

import { ForkConflictDialog } from "@/components/topology-editor/ForkConflictDialog";
import { STRING_DETAIL_MAX_LENGTH } from "@/lib/errors";
import type { ForkConflictDetail } from "@/types/reservation.types";

// The <dialog>-based Modal is opened by the setup.ts showModal stub, which sets
// the `open` attribute so its contents are in the accessibility tree.

const DETAIL: ForkConflictDetail = {
  message: "One or more ports are already claimed by another active reservation",
  conflicts: [
    { reservation_id: "res-aaaa1111", device_id: "dev-bbbb2222", port: "Ethernet1" },
    { reservation_id: "res-cccc3333", device_id: "dev-dddd4444", port: "Ethernet7" },
  ],
};

describe("ForkConflictDialog", () => {
  it("renders the backend message and every blocking reservation, device, and port", () => {
    render(<ForkConflictDialog open detail={DETAIL} onClose={vi.fn()} />);

    expect(
      screen.getByText("One or more ports are already claimed by another active reservation"),
    ).toBeInTheDocument();

    // Each conflict row surfaces its port, device, and holding reservation.
    expect(screen.getByText("port Ethernet1")).toBeInTheDocument();
    expect(screen.getByText("port Ethernet7")).toBeInTheDocument();
    expect(screen.getByText("dev-bbbb")).toBeInTheDocument();
    expect(screen.getByText("dev-dddd")).toBeInTheDocument();
    expect(screen.getByText("res-aaaa")).toBeInTheDocument();
    expect(screen.getByText("res-cccc")).toBeInTheDocument();
  });

  it("keeps-the-drawing guidance is shown so the user knows to rework", () => {
    render(<ForkConflictDialog open detail={DETAIL} onClose={vi.fn()} />);
    expect(screen.getByText(/Your drawing is kept on the canvas/)).toBeInTheDocument();
  });

  // Issue #1406: the narrower lets a body without `message` through, and a
  // string one may be blank or very long; the dialog's sentence is then its
  // own, or the server's cut to the shared detail bound.
  it.each([
    ["no message", undefined],
    ["a blank message", "   "],
  ])("shows its own sentence for %s", (_name, message) => {
    render(<ForkConflictDialog open detail={{ ...DETAIL, message }} onClose={vi.fn()} />);
    expect(
      screen.getByText("One or more ports are already claimed by another active reservation."),
    ).toBeInTheDocument();
    expect(screen.getByText("port Ethernet1")).toBeInTheDocument();
  });

  it("cuts a very long message to the shared detail bound", () => {
    const long = "x".repeat(STRING_DETAIL_MAX_LENGTH * 2);
    render(<ForkConflictDialog open detail={{ ...DETAIL, message: long }} onClose={vi.fn()} />);
    const shown = screen.getByText(/^x+\.\.\.$/).textContent ?? "";
    expect(shown).toHaveLength(STRING_DETAIL_MAX_LENGTH);
  });

  it("renders markup in the message as text, never as HTML", () => {
    const markup = '<img src="x" alt="injected"><b>bold</b>';
    const { container } = render(
      <ForkConflictDialog open detail={{ ...DETAIL, message: markup }} onClose={vi.fn()} />,
    );
    expect(screen.getByText(markup)).toBeInTheDocument();
    expect(container.ownerDocument.querySelector("img[alt='injected']")).toBeNull();
  });

  it("closes from the Back to editing button", () => {
    const onClose = vi.fn();
    render(<ForkConflictDialog open detail={DETAIL} onClose={onClose} />);
    fireEvent.click(screen.getByRole("button", { name: "Back to editing" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
