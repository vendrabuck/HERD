import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";

import { ForkSaveResultToast } from "@/components/topology-editor/ForkSaveResultToast";
import { skippedEdgeText } from "@/lib/forkSaveResult";
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

  it("omits the element attachments clause when the count is undefined or zero", () => {
    const { rerender } = render(<ForkSaveResultToast result={RESULT} onDismiss={vi.fn()} />);
    expect(screen.queryByText(/element attachment/)).not.toBeInTheDocument();

    rerender(
      <ForkSaveResultToast
        result={{ ...RESULT, element_attachments_skipped: 0 }}
        onDismiss={vi.fn()}
      />,
    );
    expect(screen.queryByText(/element attachment/)).not.toBeInTheDocument();
  });

  it("shows the element attachments clause when the count is greater than zero", () => {
    render(
      <ForkSaveResultToast
        result={{ ...RESULT, element_attachments_skipped: 3 }}
        onDismiss={vi.fn()}
      />,
    );
    expect(screen.getByText("3 element attachments recorded (not wired)")).toBeInTheDocument();
  });

  it("names each line the save could not wire on its chosen ports (issue #1007)", () => {
    render(
      <ForkSaveResultToast
        result={{
          ...RESULT,
          constrained_edges_skipped: [
            {
              edge_id: "e1",
              source_device_id: "aaaaaaaa1111",
              target_device_id: "bbbbbbbb2222",
              source_port_name: "eth9",
              target_port_name: "eth8",
            },
          ],
        }}
        deviceLabels={{ aaaaaaaa1111: "leaf-1", bbbbbbbb2222: "spine-1" }}
        onDismiss={vi.fn()}
      />,
    );
    // Shown without expanding the detail, as an alert.
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(
      screen.getByText("1 line not wired: no cable path on the chosen ports"),
    ).toBeInTheDocument();
    expect(screen.getByText("leaf-1 eth9 to spine-1 eth8")).toBeInTheDocument();
  });

  it("pluralizes and falls back to short ids and 'any port'", () => {
    render(
      <ForkSaveResultToast
        result={{
          ...RESULT,
          constrained_edges_skipped: [
            {
              edge_id: "e1",
              source_device_id: "aaaaaaaa1111",
              target_device_id: "bbbbbbbb2222",
              source_port_name: "eth9",
              target_port_name: null,
            },
            {
              edge_id: null,
              source_device_id: "aaaaaaaa1111",
              target_device_id: "cccccccc3333",
              source_port_name: null,
              target_port_name: "eth3",
            },
          ],
        }}
        onDismiss={vi.fn()}
      />,
    );
    expect(
      screen.getByText("2 lines not wired: no cable path on the chosen ports"),
    ).toBeInTheDocument();
    expect(screen.getByText("aaaaaaaa eth9 to bbbbbbbb any port")).toBeInTheDocument();
    expect(screen.getByText("aaaaaaaa any port to cccccccc eth3")).toBeInTheDocument();
  });

  it("omits the not-wired block when the list is absent or empty", () => {
    const { rerender } = render(<ForkSaveResultToast result={RESULT} onDismiss={vi.fn()} />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    rerender(
      <ForkSaveResultToast
        result={{ ...RESULT, constrained_edges_skipped: [] }}
        onDismiss={vi.fn()}
      />,
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText(/not wired/)).not.toBeInTheDocument();
  });

  it("skippedEdgeText uses the label map and defaults to an empty one", () => {
    const edge = {
      edge_id: "e1",
      source_device_id: "dev-a",
      target_device_id: "0123456789ab",
      source_port_name: "p1",
      target_port_name: "p2",
    };
    expect(skippedEdgeText(edge)).toBe("dev-a p1 to 01234567 p2");
    expect(skippedEdgeText(edge, { "dev-a": "A", "0123456789ab": "B" })).toBe("A p1 to B p2");
  });
});
