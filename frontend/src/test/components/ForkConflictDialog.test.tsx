import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";

import { ForkConflictDialog } from "@/components/topology-editor/ForkConflictDialog";
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

  it("closes from the Back to editing button", () => {
    const onClose = vi.fn();
    render(<ForkConflictDialog open detail={DETAIL} onClose={onClose} />);
    fireEvent.click(screen.getByRole("button", { name: "Back to editing" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
