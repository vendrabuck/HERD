import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { TransferList } from "@/components/ui/TransferList";

const available = [
  { id: "1", label: "Alice", sublabel: "alice@test.com" },
  { id: "2", label: "Bob", sublabel: "bob@test.com" },
  { id: "3", label: "Charlie", sublabel: "charlie@test.com" },
];

const assigned = [
  { id: "4", label: "Diana", sublabel: "diana@test.com" },
];

describe("TransferList", () => {
  it("renders items on both sides", () => {
    render(
      <TransferList
        availableItems={available}
        assignedItems={assigned}
        onAssign={vi.fn()}
        onUnassign={vi.fn()}
      />
    );

    expect(screen.getByText("Alice")).toBeDefined();
    expect(screen.getByText("Bob")).toBeDefined();
    expect(screen.getByText("Charlie")).toBeDefined();
    expect(screen.getByText("Diana")).toBeDefined();
  });

  it("shows custom labels", () => {
    render(
      <TransferList
        availableItems={available}
        assignedItems={assigned}
        onAssign={vi.fn()}
        onUnassign={vi.fn()}
        availableLabel="Not Members"
        assignedLabel="Members"
      />
    );

    expect(screen.getByText("Not Members")).toBeDefined();
    expect(screen.getByText("Members")).toBeDefined();
  });

  it("filters items with search", () => {
    render(
      <TransferList
        availableItems={available}
        assignedItems={assigned}
        onAssign={vi.fn()}
        onUnassign={vi.fn()}
      />
    );

    const searchInputs = screen.getAllByPlaceholderText("Search...");
    fireEvent.change(searchInputs[0], { target: { value: "ali" } });

    // Alice should be visible, Bob and Charlie should be filtered out
    expect(screen.getByText("Alice")).toBeDefined();
    expect(screen.queryByText("Bob")).toBeNull();
    expect(screen.queryByText("Charlie")).toBeNull();
  });

  it("assigns selected items when right arrow clicked", () => {
    const onAssign = vi.fn();
    render(
      <TransferList
        availableItems={available}
        assignedItems={assigned}
        onAssign={onAssign}
        onUnassign={vi.fn()}
      />
    );

    // Select Alice
    const checkboxes = screen.getAllByRole("checkbox");
    fireEvent.click(checkboxes[0]); // Alice

    // Click assign button (right arrow)
    const buttons = screen.getAllByRole("button");
    const assignBtn = buttons[0]; // first button is right arrow
    fireEvent.click(assignBtn);

    expect(onAssign).toHaveBeenCalledWith(["1"]);
  });

  it("unassigns selected items when left arrow clicked", () => {
    const onUnassign = vi.fn();
    render(
      <TransferList
        availableItems={available}
        assignedItems={assigned}
        onAssign={vi.fn()}
        onUnassign={onUnassign}
      />
    );

    // Select Diana (assigned side)
    const checkboxes = screen.getAllByRole("checkbox");
    const dianaCheckbox = checkboxes[checkboxes.length - 1]; // last checkbox
    fireEvent.click(dianaCheckbox);

    // Click unassign button (left arrow)
    const buttons = screen.getAllByRole("button");
    const unassignBtn = buttons[1]; // second button is left arrow
    fireEvent.click(unassignBtn);

    expect(onUnassign).toHaveBeenCalledWith(["4"]);
  });

  it("disables buttons when nothing selected", () => {
    render(
      <TransferList
        availableItems={available}
        assignedItems={assigned}
        onAssign={vi.fn()}
        onUnassign={vi.fn()}
      />
    );

    const buttons = screen.getAllByRole("button");
    expect(buttons[0]).toHaveProperty("disabled", true);
    expect(buttons[1]).toHaveProperty("disabled", true);
  });

  it("shows loading state", () => {
    render(
      <TransferList
        availableItems={[]}
        assignedItems={[]}
        onAssign={vi.fn()}
        onUnassign={vi.fn()}
        isLoading
      />
    );

    expect(screen.getByText("Loading...")).toBeDefined();
  });
});
