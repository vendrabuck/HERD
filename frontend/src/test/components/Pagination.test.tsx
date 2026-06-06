import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { Pagination } from "@/components/ui/Pagination";

describe("Pagination", () => {
  it("renders nothing when total fits on one page", () => {
    const { container } = render(
      <Pagination total={30} skip={0} limit={50} onPageChange={vi.fn()} />
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders when total exceeds limit", () => {
    render(
      <Pagination total={100} skip={0} limit={50} onPageChange={vi.fn()} />
    );
    expect(screen.getByText("Showing 1-50 of 100")).toBeInTheDocument();
    expect(screen.getByText("Page 1 of 2")).toBeInTheDocument();
  });

  it("disables Prev on first page", () => {
    render(
      <Pagination total={100} skip={0} limit={50} onPageChange={vi.fn()} />
    );
    expect(screen.getByText("Prev")).toBeDisabled();
    expect(screen.getByText("Next")).not.toBeDisabled();
  });

  it("disables Next on last page", () => {
    render(
      <Pagination total={100} skip={50} limit={50} onPageChange={vi.fn()} />
    );
    expect(screen.getByText("Prev")).not.toBeDisabled();
    expect(screen.getByText("Next")).toBeDisabled();
    expect(screen.getByText("Showing 51-100 of 100")).toBeInTheDocument();
    expect(screen.getByText("Page 2 of 2")).toBeInTheDocument();
  });

  it("calls onPageChange with correct skip on Next", () => {
    const onPageChange = vi.fn();
    render(
      <Pagination total={150} skip={0} limit={50} onPageChange={onPageChange} />
    );
    fireEvent.click(screen.getByText("Next"));
    expect(onPageChange).toHaveBeenCalledWith(50);
  });

  it("calls onPageChange with correct skip on Prev", () => {
    const onPageChange = vi.fn();
    render(
      <Pagination total={150} skip={50} limit={50} onPageChange={onPageChange} />
    );
    fireEvent.click(screen.getByText("Prev"));
    expect(onPageChange).toHaveBeenCalledWith(0);
  });

  it("shows correct info for middle page", () => {
    render(
      <Pagination total={150} skip={50} limit={50} onPageChange={vi.fn()} />
    );
    expect(screen.getByText("Showing 51-100 of 150")).toBeInTheDocument();
    expect(screen.getByText("Page 2 of 3")).toBeInTheDocument();
    expect(screen.getByText("Prev")).not.toBeDisabled();
    expect(screen.getByText("Next")).not.toBeDisabled();
  });

  it("handles partial last page", () => {
    render(
      <Pagination total={75} skip={50} limit={50} onPageChange={vi.fn()} />
    );
    expect(screen.getByText("Showing 51-75 of 75")).toBeInTheDocument();
    expect(screen.getByText("Page 2 of 2")).toBeInTheDocument();
  });
});
