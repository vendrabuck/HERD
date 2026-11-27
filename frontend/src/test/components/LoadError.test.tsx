import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { LoadError, LoadErrorNotice } from "@/components/ui/LoadError";

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

  it("Retry is type=button, so inside a form it never submits it", () => {
    const onSubmit = vi.fn((e: React.FormEvent) => e.preventDefault());
    const onRetry = vi.fn();
    render(
      <form onSubmit={onSubmit}>
        <LoadError what="drivers" error={null} onRetry={onRetry} />
      </form>,
    );
    const retry = screen.getByRole("button", { name: "Retry loading drivers" });
    expect(retry).toHaveAttribute("type", "button");
    fireEvent.click(retry);
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(onSubmit).not.toHaveBeenCalled();
  });
});

describe("LoadErrorNotice (issue #1320 review)", () => {
  const failure = { response: { status: 503, data: { detail: "inventory is restarting" } } };

  it("renders nothing while there is no data, or the latest fetch did not fail", () => {
    const refetch = vi.fn();
    const { container, rerender } = render(
      <LoadErrorNotice
        what="drivers"
        query={{ data: undefined, isError: true, error: failure, isFetching: false, refetch }}
      />,
    );
    expect(container).toBeEmptyDOMElement();
    rerender(
      <LoadErrorNotice
        what="drivers"
        query={{ data: [1], isError: false, error: null, isFetching: false, refetch }}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("says the refresh failed, politely, with the reason and a Retry that refetches", () => {
    const refetch = vi.fn();
    render(
      <LoadErrorNotice
        what="drivers"
        query={{ data: [1], isError: true, error: failure, isFetching: false, refetch }}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "Could not refresh drivers. inventory is restarting",
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    const retry = screen.getByRole("button", { name: "Retry loading drivers" });
    expect(retry).toHaveAttribute("type", "button");
    fireEvent.click(retry);
    expect(refetch).toHaveBeenCalledTimes(1);
  });

  it("while the refetch runs reads Retrying..., is aria-disabled, and ignores clicks", () => {
    const refetch = vi.fn();
    render(
      <LoadErrorNotice
        what="drivers"
        query={{ data: [1], isError: true, error: failure, isFetching: true, refetch }}
      />,
    );
    const retry = screen.getByRole("button", { name: "Retrying..." });
    expect(retry).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(retry);
    expect(refetch).not.toHaveBeenCalled();
  });

  it("stays silent when the kept data has an unusable shape (the page shows the full error)", () => {
    const { container } = render(
      <LoadErrorNotice
        what="drivers"
        query={{ data: "<html>", isError: true, error: failure, isFetching: false, refetch: vi.fn() }}
        isValid={(d: unknown) => Array.isArray(d)}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
