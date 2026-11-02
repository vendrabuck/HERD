import { http, HttpResponse } from "msw";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";

const { toastError, toastSuccess } = vi.hoisted(() => ({
  toastError: vi.fn(),
  toastSuccess: vi.fn(),
}));

vi.mock("react-hot-toast", () => ({
  default: { error: toastError, success: toastSuccess },
}));

import { server } from "../mocks/server";
import { SettingsPage } from "@/pages/SettingsPage";
import { useAuthStore } from "@/stores/authStore";
import type { User } from "@/types/auth.types";

function renderWithProviders(node: ReactNode, client?: QueryClient) {
  const qc =
    client ??
    new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>);
}

const ME: User = {
  id: "me-1",
  email: "ada@login.example.com",
  username: "ada",
  full_name: "Ada Lovelace",
  contact_email: null,
  phone: "555 0100",
  is_active: true,
  role: "user",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const DEFAULT_PREFS = {
  channels: { in_app: true },
  events: {
    "reservation.created": true,
    "reservation.updated": false,
    "reservation.cancelled": true,
    "reservation.completed": true,
  },
};

beforeEach(() => {
  toastError.mockClear();
  toastSuccess.mockClear();
  useAuthStore.setState({ isAuthenticated: false, user: null });
});

describe("SettingsPage", () => {
  it("shows a loading state then renders all event toggles", async () => {
    server.use(
      http.get("/api/notifications/notifications/preferences", () =>
        HttpResponse.json(DEFAULT_PREFS),
      ),
    );
    renderWithProviders(<SettingsPage />);
    expect(screen.getByText(/Loading preferences/i)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByText("Notifications")).toBeInTheDocument(),
    );
    expect(screen.getByText("Reservation confirmed")).toBeInTheDocument();
    expect(screen.getByText("Reservation updated")).toBeInTheDocument();
    expect(screen.getByText("Reservation cancelled")).toBeInTheDocument();
    expect(screen.getByText("Reservation completed")).toBeInTheDocument();
    expect(screen.getByText("Reservation expiring soon")).toBeInTheDocument();
    expect(screen.getByText("Reservation failed")).toBeInTheDocument();
    expect(screen.getByText("Device health changes")).toBeInTheDocument();
  });

  it("shows the failed event on by default and sends an opt-out explicitly", async () => {
    // Issue #1077: DEFAULT_PREFS predates reservation.failed, so the toggle
    // starts checked; unchecking it must send the key as false.
    let captured: unknown = null;
    server.use(
      http.get("/api/notifications/notifications/preferences", () =>
        HttpResponse.json(DEFAULT_PREFS),
      ),
      http.put(
        "/api/notifications/notifications/preferences",
        async ({ request }) => {
          const body = await request.json();
          captured = body;
          return HttpResponse.json(body);
        },
      ),
    );
    renderWithProviders(<SettingsPage />);
    await waitFor(() =>
      expect(screen.getByText("Notifications")).toBeInTheDocument(),
    );
    const failed = screen.getByLabelText("Reservation failed") as HTMLInputElement;
    expect(failed.checked).toBe(true);
    fireEvent.click(failed);
    expect(failed.checked).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith("Preferences saved"),
    );
    const body = captured as { events: Record<string, boolean> };
    expect(body.events["reservation.failed"]).toBe(false);
  });

  it("toggling the device health event sends device.health_transition in the payload", async () => {
    let captured: unknown = null;
    server.use(
      http.get("/api/notifications/notifications/preferences", () =>
        HttpResponse.json(DEFAULT_PREFS),
      ),
      http.put(
        "/api/notifications/notifications/preferences",
        async ({ request }) => {
          const body = await request.json();
          captured = body;
          return HttpResponse.json(body);
        },
      ),
    );
    renderWithProviders(<SettingsPage />);
    await waitFor(() =>
      expect(screen.getByText("Notifications")).toBeInTheDocument(),
    );

    // The prefs payload omits device.health_transition, so the component
    // defaults it on; toggling it off must send the key explicitly.
    const health = screen.getByLabelText("Device health changes") as HTMLInputElement;
    expect(health.checked).toBe(true);
    fireEvent.click(health);
    expect(health.checked).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith("Preferences saved"),
    );
    const body = captured as { events: Record<string, boolean> };
    expect(body.events["device.health_transition"]).toBe(false);
  });

  it("renders outbound channel toggles defaulting off", async () => {
    server.use(
      http.get("/api/notifications/notifications/preferences", () =>
        HttpResponse.json(DEFAULT_PREFS),
      ),
    );
    renderWithProviders(<SettingsPage />);
    await waitFor(() =>
      expect(screen.getByText("Notifications")).toBeInTheDocument(),
    );
    const email = screen.getByLabelText("Email") as HTMLInputElement;
    const chat = screen.getByLabelText("Chat") as HTMLInputElement;
    const webhook = screen.getByLabelText("Webhook") as HTMLInputElement;
    // Prefs payload omits outbound channels, so the component defaults them off.
    expect(email.checked).toBe(false);
    expect(chat.checked).toBe(false);
    expect(webhook.checked).toBe(false);
  });

  it("enabling an outbound channel sends it in the save payload", async () => {
    let captured: unknown = null;
    server.use(
      http.get("/api/notifications/notifications/preferences", () =>
        HttpResponse.json(DEFAULT_PREFS),
      ),
      http.put(
        "/api/notifications/notifications/preferences",
        async ({ request }) => {
          const body = await request.json();
          captured = body;
          return HttpResponse.json(body);
        },
      ),
    );
    renderWithProviders(<SettingsPage />);
    await waitFor(() =>
      expect(screen.getByText("Notifications")).toBeInTheDocument(),
    );
    const email = screen.getByLabelText("Email") as HTMLInputElement;
    fireEvent.click(email);
    expect(email.checked).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith("Preferences saved"),
    );
    const body = captured as { channels: Record<string, boolean> };
    expect(body.channels.email).toBe(true);
    expect(body.channels.in_app).toBe(true);
    expect(body.channels.chat).toBe(false);
    expect(body.channels.webhook).toBe(false);
  });

  it("toggling and saving sends the new payload to the API", async () => {
    let captured: unknown = null;
    server.use(
      http.get("/api/notifications/notifications/preferences", () =>
        HttpResponse.json(DEFAULT_PREFS),
      ),
      http.put(
        "/api/notifications/notifications/preferences",
        async ({ request }) => {
          const body = await request.json();
          captured = body;
          return HttpResponse.json(body);
        },
      ),
    );
    renderWithProviders(<SettingsPage />);
    await waitFor(() =>
      expect(screen.getByText("Notifications")).toBeInTheDocument(),
    );

    // Reflect the in_app checkbox starting checked
    const inAppCheckbox = screen.getByLabelText(
      "Show in-app notifications",
    ) as HTMLInputElement;
    expect(inAppCheckbox.checked).toBe(true);
    fireEvent.click(inAppCheckbox);
    expect(inAppCheckbox.checked).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith("Preferences saved"),
    );
    const body = captured as {
      channels: { in_app: boolean };
      events: Record<string, boolean>;
    };
    expect(body.channels.in_app).toBe(false);
    expect(body.events["reservation.updated"]).toBe(false);
  });

  it("toasts an error when the save call fails", async () => {
    server.use(
      http.get("/api/notifications/notifications/preferences", () =>
        HttpResponse.json(DEFAULT_PREFS),
      ),
      http.put("/api/notifications/notifications/preferences", () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    renderWithProviders(<SettingsPage />);
    await waitFor(() =>
      expect(screen.getByText("Notifications")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Failed to save preferences"),
    );
  });
});


// Issue #1252: the Profile section above Notifications.
describe("SettingsPage profile section", () => {
  function profileHandlers(onPatch?: (body: unknown) => Response | Promise<Response>) {
    useAuthStore.setState({ isAuthenticated: true, accessToken: "t" });
    server.use(
      http.get("/api/notifications/notifications/preferences", () =>
        HttpResponse.json(DEFAULT_PREFS),
      ),
      http.get("/api/auth/me", () => HttpResponse.json(ME)),
      http.patch("/api/auth/me/profile", async ({ request }) => {
        const body = await request.json();
        return onPatch
          ? onPatch(body)
          : HttpResponse.json({ ...ME, ...(body as object), updated_at: "2026-02-02T00:00:00Z" });
      }),
    );
  }

  it("prefills the editable fields and shows the login identity read-only", async () => {
    profileHandlers();
    renderWithProviders(<SettingsPage />);
    const name = (await screen.findByLabelText("Name")) as HTMLInputElement;
    expect(name.value).toBe("Ada Lovelace");
    expect((screen.getByLabelText("Contact email") as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("Phone") as HTMLInputElement).value).toBe("555 0100");
    expect(screen.getByText("ada@login.example.com")).toBeInTheDocument();
    expect(screen.getByText("ada")).toBeInTheDocument();
    // Login email and username are not inputs.
    expect(screen.queryByDisplayValue("ada@login.example.com")).not.toBeInTheDocument();
    expect(
      screen.getByText("Where email notifications go. Leave blank to use the sign-in email."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Stored and shown only. HERD does not call or text this number."),
    ).toBeInTheDocument();
  });

  it("disables Save profile until a field changes and while the name is blank", async () => {
    profileHandlers();
    renderWithProviders(<SettingsPage />);
    const name = await screen.findByLabelText("Name");
    const save = screen.getByRole("button", { name: "Save profile" });
    expect(save).toBeDisabled();
    fireEvent.change(name, { target: { value: "   " } });
    expect(save).toBeDisabled();
    expect(screen.getByText("Name is required.")).toBeInTheDocument();
    fireEvent.change(name, { target: { value: "Countess Ada" } });
    expect(save).toBeEnabled();
  });

  it("PATCHes only the changed fields and refreshes the cached current user", async () => {
    let captured: unknown = null;
    profileHandlers((body) => {
      captured = body;
      return HttpResponse.json({
        ...ME,
        full_name: "Countess Ada",
        contact_email: "ada@mail.example.com",
        updated_at: "2026-02-02T00:00:00Z",
      });
    });
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    renderWithProviders(<SettingsPage />, client);
    fireEvent.change(await screen.findByLabelText("Name"), {
      target: { value: " Countess Ada " },
    });
    fireEvent.change(screen.getByLabelText("Contact email"), {
      target: { value: "ada@mail.example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save profile" }));
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Profile saved"));
    expect(captured).toEqual({ full_name: "Countess Ada", contact_email: "ada@mail.example.com" });
    expect(client.getQueryData<User>(["currentUser"])?.full_name).toBe("Countess Ada");
    // The form remounts with the saved values as its new baseline.
    await waitFor(() =>
      expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("Countess Ada"),
    );
    expect(screen.getByRole("button", { name: "Save profile" })).toBeDisabled();
  });

  it("sends a cleared phone as null", async () => {
    let captured: unknown = null;
    profileHandlers((body) => {
      captured = body;
      return HttpResponse.json({ ...ME, phone: null });
    });
    renderWithProviders(<SettingsPage />);
    fireEvent.change(await screen.findByLabelText("Phone"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save profile" }));
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith("Profile saved"));
    expect(captured).toEqual({ phone: null });
  });

  it("toasts a 422 per field through profileSaveErrorText", async () => {
    profileHandlers(() =>
      HttpResponse.json(
        {
          detail: [
            {
              type: "string_pattern_mismatch",
              loc: ["body", "phone"],
              msg: "String should match pattern '^[0-9 +()-]*[0-9][0-9 +()-]*$'",
            },
          ],
        },
        { status: 422 },
      ),
    );
    renderWithProviders(<SettingsPage />);
    fireEvent.change(await screen.findByLabelText("Phone"), { target: { value: "call me" } });
    fireEvent.click(screen.getByRole("button", { name: "Save profile" }));
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "Phone: String should match pattern '^[0-9 +()-]*[0-9][0-9 +()-]*$'",
      ),
    );
    expect(toastSuccess).not.toHaveBeenCalled();
  });
});
