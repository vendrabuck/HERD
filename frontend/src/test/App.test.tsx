import { act, render, screen } from "@testing-library/react";
import toast from "react-hot-toast";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import App from "@/App";

// Issue #942: toasts render at the bottom centre so they never cover the
// header or the topology editor toolbar (Save sat under the old top-right
// stack). The route table is swapped for one empty route so only the shell
// mounts; the real react-hot-toast Toaster positions the toast, so this pins
// the placement through the library's own style output, not a prop snapshot.
vi.mock("@/routes", async () => {
  const { createElement } = await import("react");
  const { Route } = await import("react-router-dom");
  return { appRouteElements: createElement(Route, { path: "*", element: null }) };
});

// jsdom has no matchMedia; react-hot-toast reads prefers-reduced-motion
// through it while positioning a toast.
beforeAll(() => {
  vi.stubGlobal(
    "matchMedia",
    (query: string) =>
      ({
        matches: false,
        media: query,
        onchange: null,
        addListener: () => {},
        removeListener: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => false,
      }) as MediaQueryList,
  );
});

afterAll(() => {
  vi.unstubAllGlobals();
});

afterEach(() => {
  act(() => {
    toast.remove();
  });
});

describe("App toaster placement", () => {
  it("renders toasts at the bottom centre, not the top", async () => {
    render(<App />);
    act(() => {
      toast("Topology saved");
    });

    const message = await screen.findByText("Topology saved");
    const toaster = document.querySelector("[data-rht-toaster]");
    expect(toaster).not.toBeNull();
    // The positioned wrapper is the toaster container's direct child.
    const wrapper = Array.from(toaster!.children).find((el) => el.contains(message));
    expect(wrapper).toBeInstanceOf(HTMLElement);
    const style = (wrapper as HTMLElement).style;
    expect(style.bottom).toBe("0px");
    expect(style.top).toBe("");
    expect(style.justifyContent).toBe("center");
  });
});

describe("App root boundary and the Toaster (issue #1344)", () => {
  it("shows the last-resort page when a toast throws while rendering", async () => {
    // React and the boundary log the caught error; keep the run quiet.
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    render(<App />);
    // The #1314 crash: an object handed to a toast cannot render as a child.
    // The Toaster sits inside the root boundary, so the boundary catches it
    // instead of the whole root unmounting to a blank page.
    act(() => {
      toast.error({ loc: ["body", "name"], msg: "bad" } as unknown as string);
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("Something went wrong");
    expect(screen.getByRole("button", { name: "Reload page" })).toBeInTheDocument();
    consoleError.mockRestore();
  });
});
