/**
 * Issue #1590: the surfaces #1320 left for a follow-up. Each one, driven
 * through its real hooks and MSW, shows a failed read as the shared LoadError
 * (`Could not load <what>.` plus the reason, with Retry) in the place the read
 * would fill, never its empty text, a blank body, or a silent nothing:
 *
 * - a 500 with a detail, the frontend nginx's JSON 503 for an unrouted /api
 *   path (OPS-GUARD-8), and a 200 web-app page (ERR_UNREADABLE_ANSWER) are
 *   each a load error, and the empty text is absent;
 * - Retry with a good answer replaces the alert with the rows;
 * - only a successful empty answer shows the empty text, and no alert.
 */
import { http, HttpResponse, type RequestHandler } from "msw";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("react-hot-toast", () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

import { useAuthStore } from "@/stores/authStore";
import { server } from "../mocks/server";
import { flushPending } from "../flushPending";
import { ReservationInventoryTab } from "@/components/reservations/ReservationInventoryTab";
import { EditDevicesModal } from "@/components/reservations/EditDevicesModal";
import { DeviceSearchPicker } from "@/components/admin/connections/DeviceSearchPicker";
import { DeviceConfigSection } from "@/components/device-config/DeviceConfigSection";
import { ConnectionsPage } from "@/pages/admin/ConnectionsPage";
import type { Reservation } from "@/types/reservation.types";

const STAMP = "2026-01-01T00:00:00Z";
const DEV_ID = "dev-1";
const page = (items: unknown[]) => ({ items, total: items.length, skip: 0, limit: 50 });

type Mode = "server" | "nginx" | "html" | "list" | "empty" | "loaded";
let mode: Mode = "loaded";

const NGINX_503 =
  "This part of HERD is not answering right now. Try again in a moment.";
const SPA_PAGE =
  '<!doctype html><html><head><title>HERD</title></head><body><div id="root"></div></body></html>';

/** Answers by `mode`: a failure of one of three kinds, or `empty` or `loaded`. */
function answer(emptyBody: unknown, loadedBody: unknown) {
  switch (mode) {
    case "server":
      return HttpResponse.json({ detail: "backend is down" }, { status: 500 });
    case "nginx":
      return HttpResponse.json({ detail: NGINX_503 }, { status: 503 });
    case "html":
      return new HttpResponse(SPA_PAGE, { status: 200, headers: { "Content-Type": "text/html" } });
    case "list":
      // A readable 200 of the wrong shape: a JSON list where an object belongs.
      return HttpResponse.json([]);
    case "empty":
      return HttpResponse.json(emptyBody as never);
    default:
      return HttpResponse.json(loadedBody as never);
  }
}

const DEVICE = {
  id: DEV_ID,
  name: "core-fw-01",
  template_id: null,
  template_name: "FW-400",
  template_icon: null,
  template_vendor: null,
  template_model: null,
  template_part_number: null,
  topology_type: "PHYSICAL",
  status: "RESERVED",
  field_data: {},
  exclusive: false,
  driver_id: null,
  driver_name: null,
  connection_type: null,
  created_at: STAMP,
  updated_at: STAMP,
  created_by: null,
  created_by_name: null,
  modified_by: null,
  modified_by_name: null,
  poll_interval_seconds: null,
  resolved_poll_interval_seconds: null,
};
const PEER = { ...DEVICE, id: "dev-2", name: "edge-sw-02", status: "AVAILABLE" };
const PORT = { id: "p1", device_id: DEV_ID, name: "eth0", field_data: {}, created_at: STAMP, updated_at: STAMP };
const CABLE = {
  id: "c1",
  device_a_id: DEV_ID,
  port_a: "eth0",
  device_b_id: "dev-2",
  port_b: "eth9",
  connection_type: "ethernet",
  notes: null,
  created_by: "admin",
  created_at: STAMP,
};

const RESERVATION: Reservation = {
  id: "11111111-2222-3333-4444-555555555555",
  user_id: "user-1",
  owner_name: "alice",
  device_ids: ["dev-9"],
  topology_id: "topo-1",
  topology_type: "PHYSICAL",
  purpose: "fw test",
  start_time: "2026-06-01T00:00:00Z",
  end_time: "2026-06-02T00:00:00Z",
  status: "ACTIVE",
  created_at: "2026-05-30T00:00:00Z",
};

function version(n: number) {
  return {
    id: `ver-${n}`,
    device_id: DEV_ID,
    version_number: n,
    connection_type: "ssh",
    description: null,
    created_by: "abcdef0123456789",
    author_name: "alice",
    created_at: STAMP,
    restored_from_id: null,
    last_apply_run_id: null,
  };
}

/** /inventory/devices: the all-names walk (limit 500) always answers; any other limit is the surface's own read. */
function devicesBy(limit: string, emptyBody: unknown, loadedBody: unknown): RequestHandler {
  return http.get("/api/inventory/devices", ({ request }) => {
    const asked = new URL(request.url).searchParams.get("limit");
    if (asked === "500") return HttpResponse.json({ items: [DEVICE, PEER], total: 2, skip: 0, limit: 500 });
    if (asked !== limit) return HttpResponse.json({ detail: `unexpected limit ${asked}` }, { status: 400 });
    return answer(emptyBody, loadedBody);
  });
}

interface Spec {
  name: string;
  element: () => ReactElement;
  handlers: () => RequestHandler[];
  /** Opens or types into the surface so its read starts and shows. */
  open?: () => Promise<void>;
  what: string;
  /** Shown for a successful empty answer only; null when the surface has none. */
  emptyText: string | null;
  /** Shown once the read answers with rows. */
  loadedText: string;
  /** A modal over the version table: the failed read fills the modal body, not the table. */
  modal?: boolean;
}

const configBase = () => [
  http.get(`/api/inventory/devices/${DEV_ID}/apply-jobs`, () => HttpResponse.json(page([]))),
];

const SPECS: Spec[] = [
  {
    name: "ReservationInventoryTab ports",
    element: () => <ReservationInventoryTab deviceIds={[DEV_ID]} />,
    handlers: () => [
      http.get("/api/inventory/devices", () => HttpResponse.json({ items: [DEVICE, PEER], total: 2, skip: 0, limit: 500 })),
      http.get(`/api/inventory/devices/${DEV_ID}`, () => HttpResponse.json(DEVICE)),
      http.get("/api/cabling/connections", () => HttpResponse.json(page([]))),
      http.get(`/api/inventory/devices/${DEV_ID}/ports`, () => answer([], [PORT])),
    ],
    open: async () => {
      fireEvent.click(await screen.findByRole("button", { name: /^core-fw-01/ }));
    },
    what: "this device's ports",
    emptyText: "No ports",
    loadedText: "eth0",
  },
  {
    name: "ReservationInventoryTab connections",
    element: () => <ReservationInventoryTab deviceIds={[DEV_ID]} />,
    handlers: () => [
      http.get("/api/inventory/devices", () => HttpResponse.json({ items: [DEVICE, PEER], total: 2, skip: 0, limit: 500 })),
      http.get(`/api/inventory/devices/${DEV_ID}`, () => HttpResponse.json(DEVICE)),
      http.get(`/api/inventory/devices/${DEV_ID}/ports`, () => HttpResponse.json([PORT])),
      http.get("/api/cabling/connections", () => answer(page([]), page([CABLE]))),
    ],
    open: async () => {
      fireEvent.click(await screen.findByRole("button", { name: /^core-fw-01/ }));
    },
    what: "this device's connections",
    // A successful empty answer shows the port as unconnected; a failed one
    // must show no port row at all (checked by the table assertion below).
    emptyText: null,
    loadedText: "edge-sw-02, eth9",
  },
  {
    name: "EditDevicesModal available devices",
    element: () => <EditDevicesModal reservation={RESERVATION} open onClose={() => {}} onUpdated={() => {}} />,
    handlers: () => [devicesBy("50", page([]), page([DEVICE]))],
    what: "available devices",
    emptyText: "No available devices found",
    loadedText: "core-fw-01",
  },
  {
    name: "DeviceSearchPicker",
    element: () => (
      <DeviceSearchPicker label="Device A" device={null} onSelect={() => {}} onClear={() => {}} />
    ),
    handlers: () => [devicesBy("20", page([]), page([DEVICE]))],
    open: async () => {
      fireEvent.change(screen.getByRole("textbox", { name: "Search Device A" }), {
        target: { value: "core" },
      });
    },
    what: "matching devices",
    emptyText: "No devices match",
    loadedText: "core-fw-01",
  },
  {
    name: "ConnectionsPage device filter typeahead",
    element: () => <ConnectionsPage />,
    handlers: () => [
      http.get("/api/cabling/connections", () => HttpResponse.json(page([]))),
      devicesBy("20", page([]), page([DEVICE])),
    ],
    open: async () => {
      fireEvent.change(screen.getByPlaceholderText("Filter by device name..."), {
        target: { value: "c" },
      });
    },
    what: "matching devices",
    emptyText: "No devices match",
    loadedText: "core-fw-01",
  },
  ...(["A", "B"] as const).map(
    (side, index): Spec => ({
      name: `ConnectionsPage single-pair Device ${side} search`,
      element: () => <ConnectionsPage />,
      handlers: () => [
        http.get("/api/cabling/connections", () => HttpResponse.json(page([]))),
        devicesBy("20", page([]), page([DEVICE])),
      ],
      open: async () => {
        fireEvent.click(screen.getByRole("button", { name: "Single" }));
        fireEvent.click(screen.getByRole("button", { name: "Create Connection" }));
        const search = screen.getAllByPlaceholderText("Search devices...")[index];
        fireEvent.change(search, { target: { value: "core" } });
      },
      what: "matching devices",
      emptyText: "No devices match",
      loadedText: "core-fw-01",
    }),
  ),
  {
    name: "DeviceConfigSection version list",
    element: () => <DeviceConfigSection deviceId={DEV_ID} />,
    handlers: () => [
      ...configBase(),
      http.get(`/api/inventory/devices/${DEV_ID}/config-versions`, () =>
        answer(page([]), page([version(1)])),
      ),
    ],
    what: "config versions",
    emptyText: "No config versions yet.",
    loadedText: "v1",
  },
  {
    name: "DeviceConfigSection View modal",
    element: () => <DeviceConfigSection deviceId={DEV_ID} />,
    handlers: () => [
      ...configBase(),
      http.get(`/api/inventory/devices/${DEV_ID}/config-versions`, () =>
        HttpResponse.json(page([version(1)])),
      ),
      http.get(`/api/inventory/devices/${DEV_ID}/config-versions/ver-1`, () =>
        answer({}, { ...version(1), config: { hostname: "core-fw-01-config" } }),
      ),
    ],
    open: async () => {
      fireEvent.click(await screen.findByRole("button", { name: "View" }));
    },
    what: "this config version",
    modal: true,
    emptyText: null,
    loadedText: '"hostname": "core-fw-01-config"',
  },
  {
    name: "DeviceConfigSection diff modal",
    element: () => <DeviceConfigSection deviceId={DEV_ID} />,
    handlers: () => [
      ...configBase(),
      http.get(`/api/inventory/devices/${DEV_ID}/config-versions`, () =>
        HttpResponse.json(page([version(2), version(1)])),
      ),
      http.get(`/api/inventory/devices/${DEV_ID}/config-versions/diff`, () =>
        answer({}, { version_a: "ver-1", version_b: "ver-2", diff: "+hostname edge" }),
      ),
    ],
    open: async () => {
      fireEvent.click(await screen.findByLabelText("Compare v1"));
      fireEvent.click(screen.getByLabelText("Compare v2"));
      fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    },
    what: "the config diff",
    modal: true,
    emptyText: null,
    loadedText: "+hostname edge",
  },
];

function renderSpec(spec: Spec) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  server.use(...spec.handlers());
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{spec.element()}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mode = "loaded";
  useAuthStore.setState({
    user: { id: "1", role: "admin", username: "admin", email: "a@b.c" },
    accessToken: "t",
    isAuthenticated: true,
  } as never);
});

const FAILURES: Array<[Mode, string]> = [
  ["server", "backend is down"],
  ["nginx", NGINX_503],
  ["html", "The server's answer could not be read."],
];

describe.each(SPECS)("$name (issue #1590)", (spec) => {
  it.each(FAILURES)("a %s failure shows the load error with its reason, never the empty text", async (failure, reason) => {
    mode = failure;
    renderSpec(spec);
    if (spec.open) await spec.open();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(`Could not load ${spec.what}. ${reason}`);
    expect(screen.getByRole("button", { name: `Retry loading ${spec.what}` })).toBeInTheDocument();
    if (spec.emptyText) expect(screen.queryByText(spec.emptyText)).not.toBeInTheDocument();
    expect(screen.queryByText(spec.loadedText, { exact: false })).not.toBeInTheDocument();
    if (spec.modal) {
      // The alert is the modal's body: no config or diff text beside it.
      expect(alert.closest("dialog")!.querySelector("pre")).toBeNull();
    } else {
      // No port row, version row, or result list stands beside the alert.
      expect(screen.queryByRole("table")).not.toBeInTheDocument();
    }
  });

  it("Retry after a failure loads the rows and clears the alert", async () => {
    mode = "server";
    renderSpec(spec);
    if (spec.open) await spec.open();
    await screen.findByRole("alert");

    mode = "loaded";
    fireEvent.click(screen.getByRole("button", { name: `Retry loading ${spec.what}` }));
    expect(await screen.findByText(spec.loadedText, { exact: false })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  });

  if (spec.emptyText) {
    const emptyText = spec.emptyText;
    it("only a successful empty answer shows the empty text, with no alert", async () => {
      mode = "empty";
      renderSpec(spec);
      if (spec.open) await spec.open();
      expect(await screen.findByText(emptyText)).toBeInTheDocument();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });
  }
});

describe("ReservationInventoryTab connections, a successful empty answer (issue #1590)", () => {
  it("shows the port as unconnected only after the connections read answered", async () => {
    mode = "empty";
    renderSpec(SPECS[1]);
    await SPECS[1].open!();
    const row = (await screen.findByText("eth0")).closest("tr");
    expect(row).toHaveTextContent("eth0-");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("while the connections read is on its way, no port reads as unconnected", async () => {
    renderSpec(SPECS[1]);
    // Ports answer at once; the connections read never does.
    server.use(http.get("/api/cabling/connections", () => new Promise<never>(() => {})));
    await SPECS[1].open!();
    expect(await screen.findByText("Loading ports...")).toBeInTheDocument();
    await flushPending();
    expect(screen.getByText("Loading ports...")).toBeInTheDocument();
    expect(screen.queryByText("eth0")).not.toBeInTheDocument();
  });
});

describe("DeviceConfigSection never shows the error and the empty text together (issue #1590)", () => {
  it("a failed list read shows no count and no empty text", async () => {
    mode = "server";
    renderSpec(SPECS.find((s) => s.name === "DeviceConfigSection version list")!);
    await screen.findByRole("alert");
    expect(screen.queryByText("No config versions yet.")).not.toBeInTheDocument();
    expect(screen.queryByText(/^\d+ versions?$/)).not.toBeInTheDocument();
  });
});

describe.each(SPECS.filter((s) => s.modal))("$name, an answer that is not an object (issue #1590)", (spec) => {
  it("a 200 JSON list shows the load error, never an empty dialog", async () => {
    mode = "list";
    renderSpec(spec);
    await spec.open!();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(`Could not load ${spec.what}. The server's answer could not be read.`);
    expect(alert.closest("dialog")!.querySelector("pre")).toBeNull();
  });
});

describe("DeviceConfigSection View, Retry while the read is on its way (issue #1590)", () => {
  it("the Retry button reads Retrying... until the read answers", async () => {
    mode = "list";
    const spec = SPECS.find((s) => s.name === "DeviceConfigSection View modal")!;
    renderSpec(spec);
    await spec.open!();
    await screen.findByRole("alert");

    server.use(
      http.get(`/api/inventory/devices/${DEV_ID}/config-versions/ver-1`, () => new Promise<never>(() => {})),
    );
    fireEvent.click(screen.getByRole("button", { name: `Retry loading ${spec.what}` }));
    expect(await screen.findByRole("button", { name: "Retrying..." })).toHaveAttribute("aria-disabled", "true");
  });
});
