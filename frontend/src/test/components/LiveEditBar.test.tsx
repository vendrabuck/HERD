import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";

import { LiveEditBar } from "@/components/topology-editor/LiveEditBar";

function makeBar(overrides: Partial<Parameters<typeof LiveEditBar>[0]> = {}) {
  const onCommit = vi.fn();
  const onCancel = vi.fn();
  render(
    <LiveEditBar
      deviceCount={3}
      invalidEdgeCount={0}
      isCommitting={false}
      onCommit={onCommit}
      onCancel={onCancel}
      {...overrides}
    />,
  );
  return { onCommit, onCancel };
}

describe("LiveEditBar", () => {
  it("renders the live-edit banner and device count", () => {
    makeBar();
    expect(screen.getByText("EDITING LIVE RESERVATION")).toBeInTheDocument();
    expect(screen.getByText("3 devices")).toBeInTheDocument();
  });

  it("uses singular for a single device", () => {
    makeBar({ deviceCount: 1 });
    expect(screen.getByText("1 device")).toBeInTheDocument();
  });

  it("wires Commit and Cancel to their callbacks when valid", () => {
    const { onCommit, onCancel } = makeBar();
    fireEvent.click(screen.getByRole("button", { name: "Commit to reservation" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("blocks commit and warns when an edge is unreachable", () => {
    const { onCommit } = makeBar({ invalidEdgeCount: 2 });
    expect(
      screen.getByText("2 edges have no physical path; fix or remove them before committing."),
    ).toBeInTheDocument();
    const commit = screen.getByRole("button", { name: "Commit to reservation" });
    expect(commit).toBeDisabled();
    fireEvent.click(commit);
    expect(onCommit).not.toHaveBeenCalled();
  });

  it("shows a committing state and disables both actions", () => {
    const { onCommit, onCancel } = makeBar({ isCommitting: true });
    const commit = screen.getByRole("button", { name: "Committing..." });
    expect(commit).toBeDisabled();
    fireEvent.click(commit);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCommit).not.toHaveBeenCalled();
    expect(onCancel).not.toHaveBeenCalled();
  });
});
