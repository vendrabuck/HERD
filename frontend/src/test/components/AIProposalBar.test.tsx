import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";

import { AIProposalBar } from "@/components/topology-editor/AIProposalBar";

function makeBar(overrides: Partial<Parameters<typeof AIProposalBar>[0]> = {}) {
  const onAccept = vi.fn();
  const onModify = vi.fn();
  const onReject = vi.fn();
  render(
    <AIProposalBar
      purpose="HA pair"
      deviceCount={2}
      edgeCount={1}
      notes="firewalls only"
      onAccept={onAccept}
      onModify={onModify}
      onReject={onReject}
      {...overrides}
    />,
  );
  return { onAccept, onModify, onReject };
}

describe("AIProposalBar", () => {
  it("renders purpose, counts, and notes", () => {
    makeBar();
    expect(screen.getByText("HA pair")).toBeInTheDocument();
    expect(screen.getByText("2 devices, 1 edge. Review before committing.")).toBeInTheDocument();
    expect(screen.getByText("firewalls only")).toBeInTheDocument();
  });

  it("uses singular when device count is 1 and edge count is 1", () => {
    makeBar({ deviceCount: 1, edgeCount: 1 });
    expect(screen.getByText("1 device, 1 edge. Review before committing.")).toBeInTheDocument();
  });

  it("falls back to 'Untitled proposal' when purpose is empty", () => {
    makeBar({ purpose: "" });
    expect(screen.getByText("Untitled proposal")).toBeInTheDocument();
  });

  it("wires Accept, Modify, and Reject buttons to their callbacks", () => {
    const { onAccept, onModify, onReject } = makeBar();
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    fireEvent.click(screen.getByRole("button", { name: "Modify" }));
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(onAccept).toHaveBeenCalledTimes(1);
    expect(onModify).toHaveBeenCalledTimes(1);
    expect(onReject).toHaveBeenCalledTimes(1);
  });
});
