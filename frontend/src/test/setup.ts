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

beforeAll(() => server.listen({ onUnhandledRequest: "bypass" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
