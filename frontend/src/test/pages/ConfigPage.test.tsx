import { delay, http, HttpResponse } from "msw";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const { toastError, toastSuccess } = vi.hoisted(() => ({
  toastError: vi.fn(),
  toastSuccess: vi.fn(),
}));

vi.mock("react-hot-toast", () => ({
  default: { error: toastError, success: toastSuccess },
}));

import { server } from "../mocks/server";
import { ConfigPage } from "@/pages/ConfigPage";
import { useConfigStore } from "@/stores/configStore";
import { TOAST_CLEARANCE_CLASS } from "@/lib/toastClearance";
import {
  expectAuthorCredit,
  expectCreditStackedUnderCard,
  tabToAuthorCredit,
} from "../authorCredit";

function renderWithProviders(node: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  toastError.mockClear();
  toastSuccess.mockClear();
  // Each test starts with no config token in the store.
  useConfigStore.getState().clearConfigToken();
});

describe("ConfigPage (unauthenticated)", () => {
  it("renders the config login form when no token is present", () => {
    renderWithProviders(<ConfigPage />);
    expect(screen.getByText("HERD Configuration")).toBeInTheDocument();
    const pw = screen.getByLabelText("Config Password") as HTMLInputElement;
    expect(pw.placeholder).toBe("password");
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByText("Back to login")).toBeInTheDocument();
  });

  it("posts the password and stores the token on success", async () => {
    let captured: { password?: string } = {};
    server.use(
      http.post("/api/config/login", async ({ request }) => {
        captured = (await request.json()) as { password?: string };
        return HttpResponse.json({ token: "ct", password_changed: true });
      }),
    );
    renderWithProviders(<ConfigPage />);
    fireEvent.change(screen.getByLabelText("Config Password"), {
      target: { value: "password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() =>
      expect(useConfigStore.getState().configToken).toBe("ct"),
    );
    expect(captured.password).toBe("password");
  });

  it("toasts an error when the config login is rejected", async () => {
    server.use(
      http.post("/api/config/login", () =>
        HttpResponse.json({ detail: "bad" }, { status: 401 }),
      ),
    );
    renderWithProviders(<ConfigPage />);
    fireEvent.change(screen.getByLabelText("Config Password"), {
      target: { value: "wrong" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Invalid config password"),
    );
    expect(useConfigStore.getState().configToken).toBeNull();
  });
});

// Issue #1126: the config login's attempt limit (OPS-CONFIG-22) answers 429
// with Retry-After; the form says so and holds Sign in for the wait, while a
// 401 keeps the wrong-password wording.
describe("ConfigPage login attempt limit (issue #1126)", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  function submitWrongPassword() {
    fireEvent.change(screen.getByLabelText("Config Password"), {
      target: { value: "wrong" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  }

  function lockedLogin(headers: Record<string, string>) {
    let calls = 0;
    server.use(
      http.post("/api/config/login", () => {
        calls += 1;
        return HttpResponse.json(
          { detail: "Too many failed login attempts; try again later" },
          { status: 429, headers },
        );
      }),
    );
    return () => calls;
  }

  it("keeps the wrong-password wording for a 401 and leaves Sign in enabled", async () => {
    server.use(
      http.post("/api/config/login", () =>
        HttpResponse.json({ detail: "Invalid password" }, { status: 401 }),
      ),
    );
    renderWithProviders(<ConfigPage />);
    submitWrongPassword();
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Invalid config password"));
    expect(toastError).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
  });

  it("shows the wait from Retry-After and disables Sign in until it passes", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const calls = lockedLogin({ "Retry-After": "30" });
    renderWithProviders(<ConfigPage />);
    submitWrongPassword();
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "Too many failed login attempts; try again in 30 seconds",
      ),
    );
    expect(toastError).not.toHaveBeenCalledWith("Invalid config password");
    const button = screen.getByRole("button", { name: "Sign in" });
    await waitFor(() => expect(button).toBeDisabled());

    // A submit inside the wait (Enter in the field) sends nothing.
    fireEvent.submit(button.closest("form") as HTMLFormElement);
    await vi.advanceTimersByTimeAsync(29_000);
    expect(button).toBeDisabled();
    expect(calls()).toBe(1);

    await vi.advanceTimersByTimeAsync(1_000);
    await waitFor(() => expect(button).toBeEnabled());
  });

  it("uses the singular for a one-second wait", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    lockedLogin({ "Retry-After": "1" });
    renderWithProviders(<ConfigPage />);
    submitWrongPassword();
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "Too many failed login attempts; try again in 1 second",
      ),
    );
    await vi.advanceTimersByTimeAsync(1_000);
    await waitFor(() => expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled());
  });

  it("shows the server's sentence and keeps Sign in enabled when Retry-After is absent", async () => {
    lockedLogin({});
    renderWithProviders(<ConfigPage />);
    submitWrongPassword();
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Too many failed login attempts; try again later"),
    );
    expect(toastError).not.toHaveBeenCalledWith("Invalid config password");
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
  });

  it("treats a Retry-After that is not a whole number of seconds as no stated wait", async () => {
    lockedLogin({ "Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT" });
    renderWithProviders(<ConfigPage />);
    submitWrongPassword();
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Too many failed login attempts; try again later"),
    );
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
  });
});

describe("ConfigPage (editor)", () => {
  // Issue #988: the editor ends in Save, Save and Restart, and Back to login at
  // the bottom centre, where toasts appear; the page carries the bottom padding
  // that lets them scroll clear of a stack of three toasts.
  it("pads the page end so the last controls can scroll clear of the toasts (issue #988)", async () => {
    server.use(
      http.get("/api/config/status", () =>
        HttpResponse.json({ configured: true, password_changed: true }),
      ),
      http.get("/api/config/schema", () =>
        HttpResponse.json({
          fields: [
            {
              key: "LOG_LEVEL",
              label: "Log level",
              type: "string",
              required: false,
              group: "General",
              secret: false,
              description: "",
            },
          ],
        }),
      ),
      http.get("/api/config/settings", () => HttpResponse.json({ values: { LOG_LEVEL: "INFO" } })),
    );
    useConfigStore.getState().setConfigToken("ct");
    renderWithProviders(<ConfigPage />);
    const save = await screen.findByRole("button", { name: "Save and Restart" });
    const page = save.closest(".min-h-screen") as HTMLElement;
    expect(page.className.split(/\s+/)).toContain(TOAST_CLEARANCE_CLASS);
    // The author credit is on the config sign-in cards only, not the editor.
    expect(screen.queryByTestId("author-credit")).toBeNull();
  });
});

describe("ConfigPage author credit", () => {
  it("renders under the config sign-in card, after its controls in tab order", async () => {
    renderWithProviders(<ConfigPage />);
    const link = expectAuthorCredit();
    expect(link.closest("form")).toBeNull();
    expectCreditStackedUnderCard(link, "Enter the config password to continue");
    const order = await tabToAuthorCredit();
    expect(order[0]).toBe(screen.getByLabelText("Config Password"));
    expect(order[order.length - 1]).toBe(link);
    expect(order).toContain(screen.getByRole("button", { name: "Sign in" }));
    expect(order).toContain(screen.getByRole("link", { name: "Back to login" }));
  });

  it("renders under the change-password card", async () => {
    server.use(
      http.post("/api/config/login", () =>
        HttpResponse.json({ token: "ct", password_changed: false }),
      ),
    );
    renderWithProviders(<ConfigPage />);
    fireEvent.change(screen.getByLabelText("Config Password"), {
      target: { value: "password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByText("Change Config Password");
    const link = expectAuthorCredit();
    expectCreditStackedUnderCard(link, "Change Config Password");
    const order = await tabToAuthorCredit();
    expect(order[0]).toBe(screen.getByLabelText("New Password (8-32 characters)"));
    expect(order).toContain(screen.getByRole("button", { name: "Change Password" }));
    expect(order[order.length - 1]).toBe(link);
  });
});

// Issue #1452: the change-password form counts characters as the config service
// does (code points, OPS-CONFIG-8), so an emoji is one character, never two
// UTF-16 units; the page refuses exactly what the server's bound refuses, sends
// the value unchanged, and shows the server's own sentence for a refused password.
describe("ConfigPage change password (issue #1452)", () => {
  const KEY = "\u{1F511}";

  async function openPasswordChange() {
    server.use(
      http.post("/api/config/login", () =>
        HttpResponse.json({ token: "ct", password_changed: false }),
      ),
    );
    renderWithProviders(<ConfigPage />);
    fireEvent.change(screen.getByLabelText("Config Password"), {
      target: { value: "password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByText("Change Config Password");
  }

  function submitNewPassword(value: string) {
    fireEvent.change(screen.getByLabelText("New Password (8-32 characters)"), {
      target: { value },
    });
    fireEvent.change(screen.getByLabelText("Confirm Password"), { target: { value } });
    fireEvent.click(screen.getByRole("button", { name: "Change Password" }));
  }

  function capturePosts(respond: () => Response = () => HttpResponse.json({ message: "Password changed" })) {
    const posted: string[] = [];
    server.use(
      http.post("/api/config/change-password", async ({ request }) => {
        posted.push(((await request.json()) as { new_password: string }).new_password);
        return respond();
      }),
    );
    return posted;
  }

  it("keeps the placeholder and sets no UTF-16 length attribute on either input", async () => {
    await openPasswordChange();
    for (const label of ["New Password (8-32 characters)", "Confirm Password"]) {
      const input = screen.getByLabelText(label) as HTMLInputElement;
      expect(input.placeholder).toBe("password");
      expect(input.hasAttribute("maxlength")).toBe(false);
      expect(input.hasAttribute("minlength")).toBe(false);
    }
  });

  it.each([
    ["8 ASCII", "a".repeat(8)],
    ["32 ASCII", "a".repeat(32)],
    ["8 emoji", KEY.repeat(8)],
    ["32 emoji", KEY.repeat(32)],
    ["16 ASCII and 16 emoji", "a".repeat(16) + KEY.repeat(16)],
    ["4 letters with combining marks", "é".repeat(4)],
  ])("posts %s unchanged", async (_name, value) => {
    const posted = capturePosts();
    await openPasswordChange();
    submitNewPassword(value);
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Config password changed"));
    expect(posted).toEqual([value]);
    expect(toastError).not.toHaveBeenCalled();
  });

  it.each([
    ["7 ASCII", "a".repeat(7)],
    ["33 ASCII", "a".repeat(33)],
    ["7 emoji", KEY.repeat(7)],
    ["33 emoji", KEY.repeat(33)],
    ["5 ASCII and 2 emoji", "a".repeat(5) + KEY.repeat(2)],
    ["17 ASCII and 16 emoji", "a".repeat(17) + KEY.repeat(16)],
  ])("refuses %s on the page without a request", async (_name, value) => {
    const posted = capturePosts();
    await openPasswordChange();
    submitNewPassword(value);
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Password must be 8 to 32 characters"),
    );
    expect(posted).toEqual([]);
  });

  it("shows the server's sentence for a password it refuses", async () => {
    // Eight code points the page's bound accepts and the server's character rule
    // refuses (a lone surrogate): the page is never stricter than the server.
    const posted = capturePosts(() =>
      HttpResponse.json({ detail: "Password must be valid UTF-8 text" }, { status: 422 }),
    );
    await openPasswordChange();
    submitNewPassword("\uD800" + "a".repeat(7));
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Password must be valid UTF-8 text"),
    );
    expect(posted).toEqual(["\uD800" + "a".repeat(7)]);
    expect(toastSuccess).not.toHaveBeenCalled();
  });

  it.each([
    [
      "the 503 for an unusable store",
      () =>
        HttpResponse.json(
          { detail: "The config password store is unusable; the config service log names the cause" },
          { status: 503 },
        ),
    ],
    [
      "a 422 validation list",
      () =>
        HttpResponse.json(
          { detail: [{ type: "string_type", loc: ["body", "new_password"], msg: "Input should be a valid string" }] },
          { status: 422 },
        ),
    ],
    ["a 500", () => new HttpResponse(null, { status: 500 })],
  ])("keeps the generic toast for %s", async (_name, respond) => {
    capturePosts(respond);
    await openPasswordChange();
    submitNewPassword("a".repeat(8));
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Failed to change password"));
    expect(toastError).toHaveBeenCalledTimes(1);
  });
});

// Issue #1433: Save and Restart's verdict comes from whether the restart finished,
// not from whether the one POST /apply survived the network change the restart
// itself causes. A lost answer (no response, or a 502, 503, or 504 from the gateway)
// is followed by reads of GET /apply/last; a refusal from the config service is a
// failure at once.

type Last = { id: string | null; state: string; result: unknown; finished_at: string | null };
type LastStep = Last | (() => Response | Promise<Response>);
const OLD_ID = "a".repeat(32);
const NEW_ID = "b".repeat(32);
const OLD_FINISHED = "2026-11-20T10:00:00+00:00";
const NEW_FINISHED = "2026-11-27T16:51:23+00:00";
const GOOD = { restarted: ["acl", "auth"], errors: [] };
const PARTIAL = { restarted: ["auth"], errors: ["Failed to restart acl: DockerAPIError"] };
const OLD_RESULT = { restarted: ["notifications"], errors: [] };
const done = (id: string, result: unknown, finished = NEW_FINISHED): Last => ({
  id,
  state: "done",
  result,
  finished_at: finished,
});
const running = (id: string): Last => ({ id, state: "running", result: null, finished_at: null });
const NO_RUN: Last = { id: null, state: "none", result: null, finished_at: null };
const OLD_DONE = done(OLD_ID, OLD_RESULT, OLD_FINISHED);

/** The heading the page gives a run it did not start, from the same local fields. */
function lastHeading(iso: string): string {
  const at = new Date(iso);
  const two = (n: number) => String(n).padStart(2, "0");
  return `Last restart, finished ${at.getFullYear()}-${two(at.getMonth() + 1)}-${two(at.getDate())} ${two(at.getHours())}:${two(at.getMinutes())}`;
}

function panel(): HTMLElement | null {
  return screen.queryByTestId("restart-result");
}

function panelHeading(): string | null {
  return panel()?.querySelector("h3")?.textContent ?? null;
}

const lostAnswer = () => HttpResponse.error();
const hangs = async () => {
  await delay("infinite");
  return HttpResponse.json({});
};
const gateway = (status: number) => () =>
  new HttpResponse("Bad Gateway", { status, headers: { "Content-Type": "text/plain" } });

/**
 * The editor's backend. `lasts` answers GET /apply/last in turn (the last one
 * repeats); each entry is a record, or a handler for a refused, lost, or held read.
 */
function stack({
  lasts,
  apply,
  save = () => HttpResponse.json({ message: "Configuration saved" }),
}: {
  lasts: LastStep[];
  apply: () => Response;
  save?: () => Response;
}) {
  const calls = { last: 0, apply: 0, save: 0 };
  server.use(
    http.get("/api/config/status", () =>
      HttpResponse.json({ configured: true, password_changed: true }),
    ),
    http.get("/api/config/schema", () =>
      HttpResponse.json({
        fields: [
          {
            key: "LOG_LEVEL",
            label: "Log level",
            type: "string",
            required: false,
            group: "General",
            secret: false,
            description: "",
          },
        ],
      }),
    ),
    http.get("/api/config/settings", () => HttpResponse.json({ values: { LOG_LEVEL: "INFO" } })),
    http.put("/api/config/settings", () => {
      calls.save += 1;
      return save();
    }),
    http.get("/api/config/apply/last", () => {
      const step = lasts[Math.min(calls.last, lasts.length - 1)];
      calls.last += 1;
      return typeof step === "function" ? step() : HttpResponse.json(step);
    }),
    http.post("/api/config/apply", () => {
      calls.apply += 1;
      return apply();
    }),
  );
  useConfigStore.getState().setConfigToken("ct");
  const view = renderWithProviders(<ConfigPage />);
  return { calls, view };
}

async function clickSaveAndRestart(calls: { last: number }) {
  const button = await screen.findByRole("button", { name: "Save and Restart" });
  // The editor's own read of the last restart on open comes first.
  await waitFor(() => expect(calls.last).toBeGreaterThanOrEqual(1));
  fireEvent.click(button);
}

/** Opens on an earlier run, so a stale panel is on screen before the click. */
async function openOnAnEarlierRun(lasts: LastStep[], apply: () => Response) {
  const opened = stack({ lasts: [OLD_DONE, ...lasts], apply });
  await waitFor(() => expect(panelHeading()).toBe(lastHeading(OLD_FINISHED)));
  return opened;
}

describe("ConfigPage Save and Restart outcome (issue #1433)", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows the answered result directly when POST /apply answers", async () => {
    const { calls } = stack({ lasts: [NO_RUN], apply: () => HttpResponse.json(GOOD) });
    await clickSaveAndRestart(calls);
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Restarted 2 services"));
    expect(panelHeading()).toBe("Restart Result");
    expect(panel()?.textContent).toContain("Restarted: acl, auth");
    // The read on open plus the read before the apply; no poll.
    expect(calls.last).toBe(2);
    expect(toastError).not.toHaveBeenCalled();
  });

  it("shows an answered result with errors as a partial failure", async () => {
    const { calls } = stack({ lasts: [NO_RUN], apply: () => HttpResponse.json(PARTIAL) });
    await clickSaveAndRestart(calls);
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Some services failed to restart"));
    expect(panelHeading()).toBe("Restart Result");
    expect(panel()?.textContent).toContain("Failed to restart acl: DockerAPIError");
  });

  it.each([
    ["a network error (ERR_NETWORK_CHANGED)", lostAnswer],
    ["a 502 from the gateway", gateway(502)],
    ["a 503 from the gateway", gateway(503)],
    ["a 504 from the gateway", gateway(504)],
  ])("reads the record after %s and shows the finished run", async (_name, apply) => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { calls } = await openOnAnEarlierRun(
      [OLD_DONE, running(NEW_ID), lostAnswer, done(NEW_ID, GOOD)],
      apply,
    );
    await clickSaveAndRestart(calls);
    await waitFor(() => expect(calls.apply).toBe(1));
    await vi.advanceTimersByTimeAsync(3 * 2_000);
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Restarted 2 services"));
    expect(panelHeading()).toBe("Restart Result");
    expect(panel()?.textContent).toContain("Restarted: acl, auth");
    expect(panel()?.textContent).not.toContain("notifications");
    expect(toastError).not.toHaveBeenCalled();
    expect(calls.apply).toBe(1);
  });

  it("reports a finished run's errors after a lost answer", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { calls } = stack({ lasts: [NO_RUN, NO_RUN, done(NEW_ID, PARTIAL)], apply: lostAnswer });
    await clickSaveAndRestart(calls);
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Some services failed to restart"));
    expect(panel()?.textContent).toContain("Failed to restart acl: DockerAPIError");
  });

  it("gives a poll read up after 10 seconds and takes the next one", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { calls } = stack({
      lasts: [NO_RUN, NO_RUN, hangs, done(NEW_ID, GOOD)],
      apply: lostAnswer,
    });
    await clickSaveAndRestart(calls);
    await waitFor(() => expect(calls.last).toBe(3));
    await vi.advanceTimersByTimeAsync(9_000);
    expect(calls.last).toBe(3);
    expect(toastSuccess).not.toHaveBeenCalledWith("Restarted 2 services");
    await vi.advanceTimersByTimeAsync(1_000 + 2_000);
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Restarted 2 services"));
    expect(calls.last).toBe(4);
  });

  it("keeps Save and Restart disabled while it waits for the result", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { calls } = stack({ lasts: [NO_RUN, NO_RUN, running(NEW_ID)], apply: lostAnswer });
    await clickSaveAndRestart(calls);
    await waitFor(() => expect(calls.last).toBeGreaterThanOrEqual(3));
    const button = screen.getByRole("button", { name: "Restarting..." });
    expect(button).toBeDisabled();
    expect(toastError).not.toHaveBeenCalled();
  });

  it.each([
    ["400 No configuration to apply", 400, { detail: "No configuration to apply" }],
    [
      "403 rotate first",
      403,
      { detail: "Change the config password before modifying or applying configuration" },
    ],
    ["401", 401, { detail: "Not authenticated" }],
    [
      "config's own 503",
      503,
      { detail: "The config password store is unusable; the config service log names the cause" },
    ],
    ["500", 500, { detail: "Internal Server Error" }],
  ])("fails at once on a refusal from the config service, and clears the earlier panel: %s", async (_name, status, body) => {
    const { calls } = await openOnAnEarlierRun([OLD_DONE], () =>
      HttpResponse.json(body, { status }),
    );
    await clickSaveAndRestart(calls);
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Failed to save or restart"));
    expect(calls.last).toBe(2);
    expect(panel()).toBeNull();
    expect(screen.getByRole("button", { name: "Save and Restart" })).toBeEnabled();
  });

  it("fails without an apply when the save is refused, and leaves the earlier run's labelled panel", async () => {
    const opened = stack({
      lasts: [OLD_DONE],
      apply: () => HttpResponse.json(GOOD),
      save: () =>
        HttpResponse.json({ detail: { errors: ["LOG_LEVEL is required"] } }, { status: 422 }),
    });
    await waitFor(() => expect(panelHeading()).toBe(lastHeading(OLD_FINISHED)));
    await clickSaveAndRestart(opened.calls);
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Failed to save or restart"));
    expect(opened.calls.apply).toBe(0);
    // Nothing was applied, so the panel still says which earlier run it is.
    expect(panelHeading()).toBe(lastHeading(OLD_FINISHED));
  });

  it("says the restart did not start, sends no apply, and clears the panel when the record cannot be read first", async () => {
    const { calls } = await openOnAnEarlierRun([lostAnswer], () => HttpResponse.json(GOOD));
    await clickSaveAndRestart(calls);
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "The restart did not start; try Save and Restart again",
      ),
    );
    expect(calls.apply).toBe(0);
    expect(panel()).toBeNull();
    expect(screen.getByRole("button", { name: "Save and Restart" })).toBeEnabled();
  });

  it("gives the read before the apply up after 10 seconds", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { calls } = stack({ lasts: [NO_RUN, hangs], apply: () => HttpResponse.json(GOOD) });
    await clickSaveAndRestart(calls);
    await waitFor(() => expect(calls.last).toBe(2));
    await vi.advanceTimersByTimeAsync(10_000);
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "The restart did not start; try Save and Restart again",
      ),
    );
    expect(calls.apply).toBe(0);
  });

  it("says the restart did not start, and clears the panel, when the record never shows a new run", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { calls } = await openOnAnEarlierRun([OLD_DONE], lostAnswer);
    await clickSaveAndRestart(calls);
    await waitFor(() => expect(calls.apply).toBe(1));
    await vi.advanceTimersByTimeAsync(12_000);
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "The restart did not start; try Save and Restart again",
      ),
    );
    expect(panel()).toBeNull();
    expect(toastSuccess).not.toHaveBeenCalledWith("Restarted 1 services");
  });

  it("reports an unknown outcome, and clears the panel, when the record cannot be read", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { calls } = await openOnAnEarlierRun(
      [
        OLD_DONE,
        running(NEW_ID),
        () => HttpResponse.json({ detail: "Invalid token" }, { status: 401 }),
      ],
      lostAnswer,
    );
    await clickSaveAndRestart(calls);
    await waitFor(() => expect(calls.apply).toBe(1));
    await vi.advanceTimersByTimeAsync(2 * 2_000);
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "The connection dropped during the restart and its result could not be read; check service health",
      ),
    );
    expect(panel()).toBeNull();
  });

  it("reports an unknown outcome, and clears the panel, when no result arrives within the wait", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { calls } = await openOnAnEarlierRun([OLD_DONE, running(NEW_ID)], lostAnswer);
    await clickSaveAndRestart(calls);
    await waitFor(() => expect(calls.apply).toBe(1));
    await vi.advanceTimersByTimeAsync(752_000);
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "The connection dropped during the restart and no result arrived in time; check service health",
      ),
    );
    expect(screen.getByRole("button", { name: "Save and Restart" })).toBeEnabled();
    expect(panel()).toBeNull();
  });

  it("stops reading, and reports nothing, when the page goes away mid-wait", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const consoleError = vi.spyOn(console, "error");
    try {
      const { calls, view } = stack({
        lasts: [NO_RUN, NO_RUN, running(NEW_ID), running(NEW_ID), done(NEW_ID, GOOD)],
        apply: lostAnswer,
      });
      await clickSaveAndRestart(calls);
      await waitFor(() => expect(calls.last).toBe(3));
      view.unmount();
      await vi.advanceTimersByTimeAsync(10 * 2_000);
      expect(calls.last).toBe(3);
      expect(toastSuccess).not.toHaveBeenCalledWith("Restarted 2 services");
      expect(toastError).not.toHaveBeenCalled();
      expect(consoleError).not.toHaveBeenCalled();
    } finally {
      consoleError.mockRestore();
    }
  });

  it("reports one run once when a click starts while the read on open is in flight", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let releaseOpenRead: () => void = () => {};
    const openRead = new Promise<void>((resolve) => (releaseOpenRead = resolve));
    const { calls } = stack({
      lasts: [
        async () => {
          await openRead;
          return HttpResponse.json(running(NEW_ID));
        },
        NO_RUN,
        running(NEW_ID),
        done(NEW_ID, GOOD),
      ],
      apply: lostAnswer,
    });
    const button = await screen.findByRole("button", { name: "Save and Restart" });
    await waitFor(() => expect(calls.last).toBe(1));
    fireEvent.click(button);
    await waitFor(() => expect(calls.apply).toBe(1));
    // The read made on open answers only now, with this click's own run.
    releaseOpenRead();
    await vi.advanceTimersByTimeAsync(5 * 2_000);
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Restarted 2 services"));
    await vi.advanceTimersByTimeAsync(5 * 2_000);
    expect(
      toastSuccess.mock.calls.filter(([text]) => text === "Restarted 2 services"),
    ).toHaveLength(1);
    expect(panelHeading()).toBe("Restart Result");
  });
});

// Issue #1433: a reload during or after a restart shows its true outcome, labelled so
// it cannot be read as the result of a click still to come.
describe("ConfigPage last restart on open (issue #1433)", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  function openEditor(lasts: LastStep[]) {
    let reads = 0;
    server.use(
      http.get("/api/config/status", () =>
        HttpResponse.json({ configured: true, password_changed: true }),
      ),
      http.get("/api/config/schema", () => HttpResponse.json({ fields: [] })),
      http.get("/api/config/settings", () => HttpResponse.json({ values: {} })),
      http.get("/api/config/apply/last", () => {
        const step = lasts[Math.min(reads, lasts.length - 1)];
        reads += 1;
        return typeof step === "function" ? step() : HttpResponse.json(step);
      }),
    );
    useConfigStore.getState().setConfigToken("ct");
    const view = renderWithProviders(<ConfigPage />);
    return { reads: () => reads, view };
  }

  it("shows a finished restart's result under its finish time, without announcing it again", async () => {
    openEditor([done("c".repeat(32), { restarted: ["auth"], errors: ["x failed"] }, OLD_FINISHED)]);
    await waitFor(() => expect(panel()).not.toBeNull());
    expect(panelHeading()).toBe(lastHeading(OLD_FINISHED));
    expect(screen.queryByText("Restart Result")).toBeNull();
    expect(screen.getByText("Restarted: auth")).toBeInTheDocument();
    expect(screen.getByText("x failed")).toBeInTheDocument();
    expect(toastSuccess).not.toHaveBeenCalled();
    expect(toastError).not.toHaveBeenCalled();
  });

  it("renders the finish time the record holds, not the time of the read", async () => {
    const later = "2026-11-27T23:05:00+00:00";
    openEditor([done("c".repeat(32), GOOD, later)]);
    await waitFor(() => expect(panelHeading()).toBe(lastHeading(later)));
    expect(lastHeading(later)).not.toBe(lastHeading(OLD_FINISHED));
  });

  it("shows nothing when no restart was recorded", async () => {
    const { reads } = openEditor([NO_RUN]);
    await screen.findByRole("button", { name: "Save and Restart" });
    await waitFor(() => expect(reads()).toBe(1));
    expect(panel()).toBeNull();
  });

  it("shows nothing and stays usable when the record cannot be read", async () => {
    const { reads } = openEditor([() => HttpResponse.error()]);
    await waitFor(() => expect(reads()).toBe(1));
    expect(await screen.findByRole("button", { name: "Save and Restart" })).toBeEnabled();
    expect(panel()).toBeNull();
    expect(toastError).not.toHaveBeenCalled();
  });

  it("waits for a running restart and then shows its result under its finish time", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const id = "d".repeat(32);
    openEditor([running(id), running(id), done(id, { restarted: ["acl"], errors: [] })]);
    expect(await screen.findByRole("button", { name: "Restarting..." })).toBeDisabled();
    await vi.advanceTimersByTimeAsync(2 * 2_000);
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Restarted 1 services"));
    expect(panelHeading()).toBe(lastHeading(NEW_FINISHED));
    expect(screen.getByText("Restarted: acl")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save and Restart" })).toBeEnabled();
  });

  it("says so when a running restart ends without a result to show", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const id = "d".repeat(32);
    openEditor([
      running(id),
      running(id),
      () => HttpResponse.json({ detail: "Invalid token" }, { status: 401 }),
    ]);
    await screen.findByRole("button", { name: "Restarting..." });
    await vi.advanceTimersByTimeAsync(2_000);
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "The restart that was running did not report a result; check service health",
      ),
    );
    expect(screen.getByRole("button", { name: "Save and Restart" })).toBeEnabled();
  });

  it("stops following a running restart, and reports nothing, when the page goes away", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const consoleError = vi.spyOn(console, "error");
    try {
      const id = "d".repeat(32);
      const { reads, view } = openEditor([
        running(id),
        running(id),
        running(id),
        done(id, GOOD),
      ]);
      await screen.findByRole("button", { name: "Restarting..." });
      await waitFor(() => expect(reads()).toBe(2));
      view.unmount();
      await vi.advanceTimersByTimeAsync(10 * 2_000);
      expect(reads()).toBe(2);
      expect(toastSuccess).not.toHaveBeenCalled();
      expect(toastError).not.toHaveBeenCalled();
      expect(consoleError).not.toHaveBeenCalled();
    } finally {
      consoleError.mockRestore();
    }
  });
});
