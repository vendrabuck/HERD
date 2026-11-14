import "@testing-library/jest-dom";
import { server } from "./mocks/server";

// jsdom does not implement HTMLDialogElement.showModal/close. Tests that
// mount the Modal component (which calls showModal in a useEffect) crash
// without these stubs. Real browsers and Playwright are unaffected.
if (typeof HTMLDialogElement !== "undefined") {
  HTMLDialogElement.prototype.showModal = function showModal() {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function close() {
    this.removeAttribute("open");
  };
}

// jsdom has no ResizeObserver. react-window (the wiring dialog's port-column
// virtualization) references it unconditionally on mount; without a stub
// every test that renders a virtualized list throws before assertions run.
// A no-op is fine here: tests drive layout via each row's own defaultHeight
// fallback, not real resize events.
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

// msw 3 renamed `onUnhandledRequest` to `onUnhandledFrame`; an unknown key is a
// type error and at runtime falls back to the "warn" default.
beforeAll(() => server.listen({ onUnhandledFrame: "bypass" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
