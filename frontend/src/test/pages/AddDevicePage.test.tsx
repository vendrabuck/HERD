import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";

import { AddDevicePage } from "@/pages/admin/AddDevicePage";

// CreateDeviceForm has its own test coverage; stub it here so this page's
// test stays scoped to the page's own toggle logic.
vi.mock("@/components/admin/CreateDeviceForm", () => ({
  CreateDeviceForm: () => <div data-testid="create-device-form" />,
}));

describe("AddDevicePage", () => {
  it("hides the form until the toggle button is clicked", () => {
    render(<AddDevicePage />);

    expect(screen.getByRole("button", { name: "Add Device" })).toBeInTheDocument();
    expect(screen.queryByTestId("create-device-form")).not.toBeInTheDocument();
  });

  it("shows the form and flips the button label on click, then hides it again", () => {
    render(<AddDevicePage />);

    fireEvent.click(screen.getByRole("button", { name: "Add Device" }));
    expect(screen.getByTestId("create-device-form")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Hide Form" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Hide Form" }));
    expect(screen.queryByTestId("create-device-form")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add Device" })).toBeInTheDocument();
  });
});
