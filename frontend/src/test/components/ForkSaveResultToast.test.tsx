import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";

import { ForkSaveResultToast } from "@/components/topology-editor/ForkSaveResultToast";
import type { ForkSaveResult } from "@/types/reservation.types";

const RESULT: ForkSaveResult = {
  fork_id: "f-1",
  version_number: 2,
  released: [
    { device_a_id: "aaaaaaaa1111", port_a: "eth1", device_b_id: "bbbbbbbb2222", port_b: "eth2", layer: "L2" },
  ],
  built: [
    { device_a_id: "aaaaaaaa1111", port_a: "eth1", device_b_id: "cccccccc3333", port_b: "eth3", layer: "L3" },
  ],
  unchanged_count: 4,
};

describe("ForkSaveResultToast", () => {
  it("shows the version and released/built/unchanged counts", () => {
    render(<ForkSaveResultToast result={RESULT} onDismiss={vi.fn()} />);
    expect(screen.getByText("Fork saved as v2")).toBeInTheDocument();
    expect(screen.getByText("Released 1, built 1, unchanged 4")).toBeInTheDocument();
    // Collapsed by default: no per-connection detail yet.
    expect(screen.queryByText("Released")).not.toBeInTheDocument();
  });

  it("expands to a per-connection release and build detail list", () => {
    render(<ForkSaveResultToast result={RESULT} onDismiss={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Show detail" }));

    expect(screen.getByText("Released")).toBeInTheDocument();
    expect(screen.getByText("Built")).toBeInTheDocument();
    // Endpoints are rendered as shortId/port to shortId/port, one per delta.
    expect(screen.getByText("aaaaaaaa/eth1 to bbbbbbbb/eth2")).toBeInTheDocument();
    expect(screen.getByText("aaaaaaaa/eth1 to cccccccc/eth3")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Hide detail" }));
    expect(screen.queryByText("Released")).not.toBeInTheDocument();
  });

  it("fires onDismiss from the close button", () => {
    const onDismiss = vi.fn();
    render(<ForkSaveResultToast result={RESULT} onDismiss={onDismiss} />);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("omits the detail toggle when there is nothing released or built", () => {
    render(
      <ForkSaveResultToast
        result={{ ...RESULT, released: [], built: [] }}
        onDismiss={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: "Show detail" })).not.toBeInTheDocument();
  });
});
