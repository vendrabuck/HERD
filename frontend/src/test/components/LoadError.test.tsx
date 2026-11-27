import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { LoadError } from "@/components/ui/LoadError";

describe("LoadError (issue #1320)", () => {
  it("is an alert naming the load, with the server's detail and a Retry", () => {
    const onRetry = vi.fn();
    render(
      <LoadError
        what="drivers"
        error={{ response: { status: 500, data: { detail: "inventory is down" } } }}
        onRetry={onRetry}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Could not load drivers. inventory is down",
    );
    const retry = screen.getByRole("button", { name: "Retry loading drivers" });
    expect(retry).toHaveTextContent("Retry");
    expect(retry).not.toHaveAttribute("aria-disabled");
    fireEvent.click(retry);
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("renders a detail that looks like markup as text", () => {
    render(
      <LoadError
        what="drivers"
        error={{ response: { status: 500, data: { detail: "<b>bold</b>" } } }}
        onRetry={() => {}}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Could not load drivers. <b>bold</b>");
    expect(screen.getByRole("alert").querySelector("b")).toBeNull();
  });

  it("while retrying reads Retrying..., is aria-disabled, keeps focus, and ignores clicks", () => {
    const onRetry = vi.fn();
    render(<LoadError what="drivers" error={null} onRetry={onRetry} retrying />);
    const retry = screen.getByRole("button", { name: "Retrying..." });
    expect(retry).toHaveAttribute("aria-disabled", "true");
    // Not the disabled attribute: a disabled button drops keyboard focus.
    expect(retry).not.toBeDisabled();
    retry.focus();
    fireEvent.click(retry);
    expect(onRetry).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(retry);
  });
});
