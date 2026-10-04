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
