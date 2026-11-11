import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ErrorBoundary } from "@/components/ui/ErrorBoundary";

function ThrowError({ message }: { message: string }): never {
  throw new Error(message);
}

describe("ErrorBoundary", () => {
  it("renders children when no error", () => {
    render(
      <ErrorBoundary>
        <p>Safe content</p>
      </ErrorBoundary>
    );
    expect(screen.getByText("Safe content")).toBeInTheDocument();
  });

  it("renders the fixed last-resort UI when a child throws, never the error text", () => {
    const consoleSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <ErrorBoundary>
        <ThrowError message="Test crash" />
      </ErrorBoundary>
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("Something went wrong")).toBeInTheDocument();
    // Issue #1344: the thrown message goes to the console only.
    expect(screen.queryByText("Test crash")).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain("Test crash");
    expect(screen.getByRole("button", { name: "Reload page" })).toBeInTheDocument();
    expect(consoleSpy).toHaveBeenCalledWith(
      "ErrorBoundary caught:",
      expect.objectContaining({ message: "Test crash" }),
      expect.anything(),
    );
    consoleSpy.mockRestore();
  });

  it("renders a caller's fallback in place of the last-resort UI", () => {
    const consoleSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <ErrorBoundary fallback={<p>page fallback</p>}>
        <ThrowError message="Test crash" />
      </ErrorBoundary>
    );
    expect(screen.getByText("page fallback")).toBeInTheDocument();
    expect(screen.queryByText("Something went wrong")).not.toBeInTheDocument();
    consoleSpy.mockRestore();
  });
});
