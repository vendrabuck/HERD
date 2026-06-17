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

function renderWithProviders(node: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

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
    expect(screen.getByText("Device health changes")).toBeInTheDocument();
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
