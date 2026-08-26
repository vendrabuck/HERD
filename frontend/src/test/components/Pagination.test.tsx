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

  it("still renders nothing without the page-size props even when total fits on one page", () => {
    const { container } = render(
      <Pagination total={30} skip={0} limit={50} onPageChange={vi.fn()} />
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders the bar with a page-size selector when total fits on one page but the selector props are given", () => {
    render(
      <Pagination
        total={30}
        skip={0}
        limit={50}
        onPageChange={vi.fn()}
        pageSizeOptions={[25, 50, 100, 200]}
        onPageSizeChange={vi.fn()}
      />
    );
    expect(screen.getByText("Showing 1-30 of 30")).toBeInTheDocument();
    const select = screen.getByLabelText("Rows per page") as HTMLSelectElement;
    expect(select).toBeInTheDocument();
    expect(select.value).toBe("50");
    expect(screen.getByText("50")).toBeInTheDocument();
  });

  it("renders nothing when total is zero even with the selector props given", () => {
    const { container } = render(
      <Pagination
        total={0}
        skip={0}
        limit={50}
        onPageChange={vi.fn()}
        pageSizeOptions={[25, 50, 100, 200]}
        onPageSizeChange={vi.fn()}
      />
    );
    expect(container.firstChild).toBeNull();
  });

  it("calls onPageSizeChange with a number when a new page size is selected", () => {
    const onPageSizeChange = vi.fn();
    render(
      <Pagination
        total={150}
        skip={0}
        limit={50}
        onPageChange={vi.fn()}
        pageSizeOptions={[25, 50, 100, 200]}
        onPageSizeChange={onPageSizeChange}
      />
    );
    const select = screen.getByLabelText("Rows per page");
    fireEvent.change(select, { target: { value: "100" } });
    expect(onPageSizeChange).toHaveBeenCalledWith(100);
    expect(onPageSizeChange.mock.calls[0][0]).toEqual(expect.any(Number));
  });

  it("folds an unlisted persisted limit into the option list, sorted ascending", () => {
    render(
      <Pagination
        total={150}
        skip={0}
        limit={30}
        onPageChange={vi.fn()}
        pageSizeOptions={[25, 50, 100, 200]}
        onPageSizeChange={vi.fn()}
      />
    );
    const select = screen.getByLabelText("Rows per page") as HTMLSelectElement;
    expect(select.value).toBe("30");
    const optionValues = Array.from(select.options).map((o) => o.value);
    expect(optionValues).toEqual(["25", "30", "50", "100", "200"]);
  });

  it("still shows the multi-page bar without a selector when only onPageSizeChange is provided", () => {
    const { container } = render(
      <Pagination
        total={30}
        skip={0}
        limit={50}
        onPageChange={vi.fn()}
        onPageSizeChange={vi.fn()}
      />
    );
    expect(container.firstChild).toBeNull();
  });
});
