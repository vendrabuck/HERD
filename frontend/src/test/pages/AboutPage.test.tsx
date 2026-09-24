import { http, HttpResponse } from "msw";
import { render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect, vi } from "vitest";

import { server } from "../mocks/server";

// Pin APP_VERSION/APP_BUILD/APP_BUILD_DATE to fixed, non-dev values for this
// file: under plain `vitest run` (no VITE_HERD_BUILD in the environment)
// __APP_BUILD__ resolves to the real "dev" fallback, which would make
// buildsDiffer() vacuously false for every case here (its whole rule is
// "never flag a dev build") and the build-skew scenarios untestable. Keeps
// sameRelease/buildsDiffer themselves real, since those are exactly what
// this file wants to exercise through the rendered page.
vi.mock("@/lib/appVersion", async () => {
  const actual = await vi.importActual<typeof import("@/lib/appVersion")>("@/lib/appVersion");
  return {
    ...actual,
    APP_VERSION: "0.6.0-dev",
    APP_BUILD: "v0.5.0-10-gaaaaaaa",
    APP_BUILD_DATE: "2026-09-10T00:00:00Z",
  };
});

import { AboutPage } from "@/pages/admin/AboutPage";
import { SERVICES } from "@/api/about";
import { APP_VERSION, APP_BUILD } from "@/lib/appVersion";

function renderWithProviders(node: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

// A stock, non-skewed /version response every service answers with unless a
// test overrides it. version/build match the frontend's own so the default
// scenario (all 12 answering, none flagged) needs no per-test boilerplate.
function mockAllServicesOk() {
  server.use(
    ...SERVICES.map((service) =>
      http.get(`/api${service.path}`, () =>
        HttpResponse.json({
          service: service.name,
          version: APP_VERSION,
          build: APP_BUILD,
          build_date: "2026-09-15T03:04:05Z",
        }),
      ),
    ),
  );
}

function rowFor(label: string): HTMLElement {
  const cell = screen.getByText(label);
  const row = cell.closest("tr");
  if (!row) throw new Error(`no <tr> ancestor for ${label}`);
  return row;
}

describe("AboutPage", () => {
  it("renders the frontend's own version, build, and build date", () => {
    mockAllServicesOk();
    renderWithProviders(<AboutPage />);
    expect(screen.getByText("Frontend")).toBeInTheDocument();
    expect(screen.getByText(APP_VERSION)).toBeInTheDocument();
    expect(screen.getByText(APP_BUILD)).toBeInTheDocument();
    // UTC and labeled, never the viewer's locale: see formatBuildDate.
    expect(screen.getByText("2026-09-10 00:00 UTC")).toBeInTheDocument();
  });

  it("renders all 12 services once every /version call answers", async () => {
    mockAllServicesOk();
    renderWithProviders(<AboutPage />);

    for (const service of SERVICES) {
      await waitFor(() => expect(within(rowFor(service.label)).getByText("reachable")).toBeInTheDocument());
    }
  });

  it("shows 'unreachable' for one failing service while the other 11 still render", async () => {
    mockAllServicesOk();
    server.use(
      http.get("/api/inventory/version", () => HttpResponse.json({ detail: "down" }, { status: 503 })),
    );
    renderWithProviders(<AboutPage />);

    await waitFor(() =>
      expect(within(rowFor("Inventory")).getByText("unreachable")).toBeInTheDocument(),
    );
    // Every other service still resolved normally.
    for (const service of SERVICES.filter((s) => s.name !== "inventory")) {
      await waitFor(() =>
        expect(within(rowFor(service.label)).getByText("reachable")).toBeInTheDocument(),
      );
    }
  });

  it("flags a service reporting a different version as 'differs'", async () => {
    mockAllServicesOk();
    server.use(
      http.get("/api/auth/version", () =>
        HttpResponse.json({
          service: "auth",
          version: "0.5.0",
          build: APP_BUILD,
          build_date: "2026-09-15T03:04:05Z",
        }),
      ),
    );
    renderWithProviders(<AboutPage />);

    await waitFor(() => expect(within(rowFor("Auth")).getByText("differs")).toBeInTheDocument());
    // A service reporting the same version as the frontend is never flagged.
    await waitFor(() =>
      expect(within(rowFor("Inventory")).getByText("reachable")).toBeInTheDocument(),
    );
    expect(within(rowFor("Inventory")).queryByText("differs")).not.toBeInTheDocument();
  });

  it("flags a service reporting a different, non-dev build as 'differs'", async () => {
    mockAllServicesOk();
    server.use(
      http.get("/api/cabling/version", () =>
        HttpResponse.json({
          service: "cabling",
          version: APP_VERSION,
          build: "v0.5.0-16-gb29c8812",
          build_date: "2026-09-15T03:04:05Z",
        }),
      ),
    );
    renderWithProviders(<AboutPage />);

    await waitFor(() => expect(within(rowFor("Cabling")).getByText("differs")).toBeInTheDocument());
  });

  it("does not flag a 'dev' build as differing, even against a different frontend build", async () => {
    mockAllServicesOk();
    server.use(
      http.get("/api/secrets/version", () =>
        HttpResponse.json({
          service: "secrets",
          version: APP_VERSION,
          build: "dev",
          build_date: null,
        }),
      ),
    );
    renderWithProviders(<AboutPage />);

    await waitFor(() =>
      expect(within(rowFor("Secrets")).getByText("reachable")).toBeInTheDocument(),
    );
    expect(within(rowFor("Secrets")).queryByText("differs")).not.toBeInTheDocument();
  });

  it("renders 'invalid response' with dashed cells and no 'differs' for a malformed 200 body (issue #874)", async () => {
    mockAllServicesOk();
    server.use(
      // A proxy or gateway serving an HTML error page with a 200 status: the
      // body fails the ServiceVersion shape check in fetchServiceVersion.
      http.get("/api/cabling/version", () => HttpResponse.text("<html>502</html>")),
    );
    renderWithProviders(<AboutPage />);

    const row = rowFor("Cabling");
    await waitFor(() =>
      expect(within(row).getByText("invalid response")).toBeInTheDocument(),
    );
    // Exact match, never confused with "reachable"/"unreachable" by a
    // substring matcher (the same trap the e2e suite documents).
    expect(within(row).queryByText("reachable", { exact: true })).not.toBeInTheDocument();
    expect(within(row).queryByText("unreachable")).not.toBeInTheDocument();
    expect(within(row).queryByText("differs")).not.toBeInTheDocument();
    // Version/build/build-date cells all read "-", the same as unreachable.
    const dashes = within(row).getAllByText("-");
    expect(dashes.length).toBe(3);
  });

  it("still renders 'unreachable' (not 'invalid response') for an ordinary transport/HTTP failure", async () => {
    mockAllServicesOk();
    server.use(
      http.get("/api/inventory/version", () => HttpResponse.json({ detail: "down" }, { status: 503 })),
    );
    renderWithProviders(<AboutPage />);

    const row = rowFor("Inventory");
    await waitFor(() => expect(within(row).getByText("unreachable")).toBeInTheDocument());
    expect(within(row).queryByText("invalid response")).not.toBeInTheDocument();
  });

  it("still flags an ordinary well-formed mismatched body as 'differs', not 'invalid response'", async () => {
    mockAllServicesOk();
    server.use(
      http.get("/api/auth/version", () =>
        HttpResponse.json({
          service: "auth",
          version: "0.5.0",
          build: APP_BUILD,
          build_date: "2026-09-15T03:04:05Z",
        }),
      ),
    );
    renderWithProviders(<AboutPage />);

    const row = rowFor("Auth");
    await waitFor(() => expect(within(row).getByText("differs")).toBeInTheDocument());
    expect(within(row).getByText("reachable")).toBeInTheDocument();
    expect(within(row).queryByText("invalid response")).not.toBeInTheDocument();
  });

  it("Refresh re-fetches every service", async () => {
    let acls = 0;
    mockAllServicesOk();
    server.use(
      http.get("/api/acl/version", () => {
        acls += 1;
        return HttpResponse.json({
          service: "acl",
          version: APP_VERSION,
          build: APP_BUILD,
          build_date: null,
        });
      }),
    );
    renderWithProviders(<AboutPage />);

    await waitFor(() => expect(within(rowFor("ACL")).getByText("reachable")).toBeInTheDocument());
    const callsAfterInitialLoad = acls;
    expect(callsAfterInitialLoad).toBeGreaterThan(0);

    screen.getByRole("button", { name: /refresh/i }).click();

    await waitFor(() => expect(acls).toBeGreaterThan(callsAfterInitialLoad));
  });
});
