/**
 * Issue #1320, secondary loads: a picker, a panel, or a sub-list that a page
 * fills from its own fetch. When that fetch fails, the place it would fill
 * shows the shared LoadError (role alert, "Could not load <what>." plus the
 * server's reason, Retry), never an empty picker that reads as "nothing
 * exists" or a "No ports configured" / "No items" line. Retry with a good
 * answer clears it. Driven through the real hooks and MSW.
 */
import { http, HttpResponse, type JsonBodyType, type RequestHandler } from "msw";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import type { ReactElement } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("react-hot-toast", () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

import toast from "react-hot-toast";
import { useAuthStore } from "@/stores/authStore";
import { server } from "../mocks/server";
import { flushPending } from "../flushPending";
import { HypervisorsPage } from "@/pages/admin/HypervisorsPage";
import { GrantsPage } from "@/pages/admin/GrantsPage";
import { AddDevicePage } from "@/pages/admin/AddDevicePage";
import { DeviceGroupDetailPage } from "@/pages/admin/DeviceGroupDetailPage";
import { LdapSyncPage } from "@/pages/admin/LdapSyncPage";
import { ConnectionsPage } from "@/pages/admin/ConnectionsPage";
import { TopologyTemplatesPage } from "@/pages/TopologyTemplatesPage";
import { InventoryPage } from "@/pages/InventoryPage";
import { PortsSection } from "@/components/devices/PortsSection";

const STAMP = "2026-01-01T00:00:00Z";
const page = (items: unknown[]) => ({ items, total: items.length, skip: 0, limit: 50 });
const ok = (path: string, body: JsonBodyType) => http.get(path, () => HttpResponse.json(body));

let failing = true;

/** Fails with a 500 carrying `detail` while `failing`, then answers `body`. */
function flaky(path: string, body: JsonBodyType, detail: string): RequestHandler {
  return http.get(path, () =>
    failing ? HttpResponse.json({ detail }, { status: 500 }) : HttpResponse.json(body),
  );
}

const DEVICE = {
  id: "dev-1",
  name: "core-fw-01",
  template_id: null,
  template_name: null,
  template_icon: null,
  template_vendor: null,
  template_model: null,
  template_part_number: null,
  topology_type: "PHYSICAL",
  status: "AVAILABLE",
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

const PORT = {
  id: "p1",
  device_id: "dev-1",
  name: "eth0",
  template_id: null,
  template_name: null,
  field_data: {},
  created_at: STAMP,
  updated_at: STAMP,
};

// A topology template with one role, so the instantiate dialog shows a device
// picker for it.
const INSTANTIATE_TEMPLATE = {
  id: "tt1",
  name: "Two Router Lab",
  description: null,
  created_by: "1",
  owner_name: "admin",
  created_at: STAMP,
  updated_at: STAMP,
  canvas_data: { nodes: [{ id: "n1", data: { device: { role: "edge" } } }], edges: [] },
};

interface SecondarySpec {
  name: string;
  route?: { path: string; url: string };
  element: () => ReactElement;
  handlers: () => RequestHandler[];
  /** Opens the dialog or panel the failed load fills, if one must be opened. */
  open?: () => Promise<void>;
  alertText: string;
  retryName: string;
  /** Text the failed load must never fall back to. */
  neverText?: string;
}

const SPECS: SecondarySpec[] = [
  {
    name: "HypervisorsPage secret picker",
    element: () => <HypervisorsPage />,
    handlers: () => [
      ok("/api/inventory/hypervisors", page([])),
      ok("/api/inventory/device-groups", page([])),
      flaky("/api/secrets/secrets", [], "secrets store sealed"),
    ],
    open: async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Register Hypervisor" }));
    },
    alertText: "Could not load secrets. secrets store sealed",
    retryName: "Retry loading secrets",
    neverText: "No secrets exist yet.",
  },
  {
    name: "HypervisorsPage device group picker",
    element: () => <HypervisorsPage />,
    handlers: () => [
      ok("/api/inventory/hypervisors", page([])),
      ok("/api/secrets/secrets", []),
      flaky("/api/inventory/device-groups", page([]), "inventory is restarting"),
    ],
    open: async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Register Hypervisor" }));
    },
    alertText: "Could not load device groups. inventory is restarting",
    retryName: "Retry loading device groups",
  },
  {
    name: "GrantsPage group picker",
    element: () => <GrantsPage />,
    handlers: () => [
      ok("/api/acl/grants", page([])),
      flaky("/api/auth/groups", page([]), "auth is restarting"),
    ],
    open: async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Create Grant" }));
    },
    alertText: "Could not load user groups. auth is restarting",
    retryName: "Retry loading user groups",
  },
  {
    name: "AddDevicePage template picker",
    element: () => <AddDevicePage />,
    handlers: () => [flaky("/api/inventory/templates", page([]), "inventory is restarting")],
    open: async () => {
      fireEvent.click(screen.getByRole("button", { name: "Add Device" }));
    },
    alertText: "Could not load device templates. inventory is restarting",
    retryName: "Retry loading device templates",
  },
  {
    name: "DeviceGroupDetailPage device picker",
    route: { path: "/admin/device-groups/:id", url: "/admin/device-groups/dg1" },
    element: () => <DeviceGroupDetailPage />,
    handlers: () => [
      ok("/api/inventory/device-groups/dg1", {
        id: "dg1",
        name: "Lab Rack",
        description: null,
        device_count: 0,
        user_group_count: 0,
        created_at: STAMP,
        devices: [],
        user_groups: [],
      }),
      ok("/api/auth/groups", page([])),
      flaky("/api/inventory/devices", page([]), "inventory is restarting"),
    ],
    open: async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Add or Remove Devices" }));
    },
    alertText: "Could not load devices. inventory is restarting",
    retryName: "Retry loading devices",
  },
  {
    name: "DeviceGroupDetailPage user group picker",
    route: { path: "/admin/device-groups/:id", url: "/admin/device-groups/dg1" },
    element: () => <DeviceGroupDetailPage />,
    handlers: () => [
      ok("/api/inventory/device-groups/dg1", {
        id: "dg1",
        name: "Lab Rack",
        description: null,
        device_count: 0,
        user_group_count: 0,
        created_at: STAMP,
        devices: [],
        user_groups: [],
      }),
      ok("/api/inventory/devices", page([])),
      flaky("/api/auth/groups", page([]), "auth is restarting"),
    ],
    open: async () => {
      fireEvent.click(
        await screen.findByRole("button", { name: "Add or Remove Permissions" }),
      );
    },
    alertText: "Could not load user groups. auth is restarting",
    retryName: "Retry loading user groups",
  },
  {
    name: "LdapSyncPage group picker",
    element: () => <LdapSyncPage />,
    handlers: () => [
      ok("/api/auth/admin/ldap-sync/status", {
        auth_method: "ldap",
        group_sync_enabled: false,
        sync_interval_seconds: 3600,
      }),
      ok("/api/auth/admin/ldap-sync/mappings", page([])),
      ok("/api/auth/admin/ldap-sync/runs", page([])),
      flaky("/api/auth/groups", page([]), "auth is restarting"),
    ],
    open: async () => {
      const create = await screen.findByRole("button", { name: "Create mapping" });
      await waitFor(() => expect(create).toBeEnabled());
      fireEvent.click(create);
    },
    alertText: "Could not load HERD groups. auth is restarting",
    retryName: "Retry loading HERD groups",
  },
  {
    name: "ConnectionsPage single-pair port picker",
    element: () => <ConnectionsPage />,
    handlers: () => [
      ok("/api/cabling/connections", page([])),
      ok("/api/inventory/devices", page([DEVICE])),
      flaky("/api/inventory/devices/dev-1/ports", [PORT], "inventory is restarting"),
    ],
    open: async () => {
      fireEvent.click(screen.getByRole("button", { name: "Single" }));
      fireEvent.click(screen.getByRole("button", { name: "Create Connection" }));
      const [searchA] = screen.getAllByPlaceholderText("Search devices...");
      fireEvent.change(searchA, { target: { value: "core" } });
      fireEvent.click(await screen.findByRole("button", { name: "core-fw-01" }));
    },
    alertText: "Could not load this device's ports. inventory is restarting",
    retryName: "Retry loading this device's ports",
    neverText: "No ports configured on this device",
  },
  {
    name: "ConnectionsPage single-pair port picker, Device B",
    element: () => <ConnectionsPage />,
    handlers: () => [
      ok("/api/cabling/connections", page([])),
      ok("/api/inventory/devices", page([DEVICE])),
      flaky("/api/inventory/devices/dev-1/ports", [PORT], "inventory is restarting"),
    ],
    open: async () => {
      fireEvent.click(screen.getByRole("button", { name: "Single" }));
      fireEvent.click(screen.getByRole("button", { name: "Create Connection" }));
      const [, searchB] = screen.getAllByPlaceholderText("Search devices...");
      fireEvent.change(searchB, { target: { value: "core" } });
      fireEvent.click(await screen.findByRole("button", { name: "core-fw-01" }));
    },
    alertText: "Could not load this device's ports. inventory is restarting",
    retryName: "Retry loading this device's ports",
    neverText: "No ports configured on this device",
  },
  {
    name: "TopologyTemplatesPage instantiate dialog device pickers",
    element: () => <TopologyTemplatesPage />,
    handlers: () => [
      ok(
        "/api/cabling/templates",
        page([
          {
            id: "tt1",
            name: "Two Router Lab",
            description: null,
            created_by: "1",
            owner_name: "admin",
            created_at: STAMP,
            updated_at: STAMP,
          },
        ]),
      ),
      ok("/api/cabling/templates/tt1", INSTANTIATE_TEMPLATE),
      flaky("/api/inventory/devices", page([DEVICE]), "inventory is restarting"),
    ],
    open: async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Use" }));
    },
    alertText: "Could not load devices. inventory is restarting",
    retryName: "Retry loading devices",
  },
  {
    name: "PortsSection add-port template picker",
    element: () => <PortsSection deviceId="dev-1" isAdmin />,
    handlers: () => [
      ok("/api/inventory/devices/dev-1/ports", [PORT]),
      ok("/api/cabling/connections", page([])),
      ok("/api/inventory/devices", page([])),
      flaky("/api/inventory/templates", page([]), "inventory is restarting"),
    ],
    open: async () => {
      fireEvent.click(await screen.findByRole("button", { name: "+ Add Port" }));
    },
    alertText: "Could not load port templates. inventory is restarting",
    retryName: "Retry loading port templates",
  },
  {
    name: "PortsSection bulk-add template picker",
    element: () => <PortsSection deviceId="dev-1" isAdmin />,
    handlers: () => [
      ok("/api/inventory/devices/dev-1/ports", [PORT]),
      ok("/api/cabling/connections", page([])),
      ok("/api/inventory/devices", page([])),
      flaky("/api/inventory/templates", page([]), "inventory is restarting"),
    ],
    open: async () => {
      fireEvent.click(await screen.findByRole("button", { name: "+ Add Multiple Ports" }));
    },
    alertText: "Could not load port templates. inventory is restarting",
    retryName: "Retry loading port templates",
  },
  {
    name: "TopologyTemplatesPage instantiate dialog",
    element: () => <TopologyTemplatesPage />,
    handlers: () => [
      ok(
        "/api/cabling/templates",
        page([
          {
            id: "tt1",
            name: "Two Router Lab",
            description: null,
            created_by: "1",
            owner_name: "admin",
            created_at: STAMP,
            updated_at: STAMP,
          },
        ]),
      ),
      ok("/api/inventory/devices", page([])),
      flaky(
        "/api/cabling/templates/tt1",
        {
          id: "tt1",
          name: "Two Router Lab",
          description: null,
          created_by: "1",
          owner_name: "admin",
          created_at: STAMP,
          updated_at: STAMP,
          canvas_data: { nodes: [], edges: [] },
        },
        "cabling is restarting",
      ),
    ],
    open: async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Use" }));
    },
    alertText: "Could not load this template. cabling is restarting",
    retryName: "Retry loading this template",
  },
  {
    name: "InventoryPage expanded row ports",
    element: () => <InventoryPage />,
    handlers: () => [
      ok("/api/inventory/devices", page([DEVICE])),
      ok("/api/inventory/templates", page([])),
      ok("/api/cabling/connections", page([])),
      ok("/api/inventory/devices/dev-1/device-groups", []),
      flaky("/api/inventory/devices/dev-1/ports", [PORT], "inventory is restarting"),
    ],
    open: async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Expand ports" }));
    },
    alertText: "Could not load this device's ports. inventory is restarting",
    retryName: "Retry loading this device's ports",
    neverText: "No ports configured",
  },
  {
    name: "InventoryPage expanded row connections",
    element: () => <InventoryPage />,
    handlers: () => [
      ok("/api/inventory/devices", page([DEVICE])),
      ok("/api/inventory/templates", page([])),
      ok("/api/inventory/devices/dev-1/ports", [PORT]),
      ok("/api/inventory/devices/dev-1/device-groups", []),
      flaky("/api/cabling/connections", page([]), "cabling is restarting"),
    ],
    open: async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Expand ports" }));
    },
    alertText: "Could not load this device's connections. cabling is restarting",
    retryName: "Retry loading this device's connections",
    neverText: "Not connected",
  },
  {
    name: "PortsSection ports",
    element: () => <PortsSection deviceId="dev-1" isAdmin />,
    handlers: () => [
      ok("/api/inventory/templates", page([])),
      ok("/api/cabling/connections", page([])),
      ok("/api/inventory/devices", page([])),
      flaky("/api/inventory/devices/dev-1/ports", [PORT], "inventory is restarting"),
    ],
    alertText: "Could not load this device's ports. inventory is restarting",
    retryName: "Retry loading this device's ports",
    neverText: "No ports configured",
  },
  {
    name: "PortsSection connections",
    element: () => <PortsSection deviceId="dev-1" isAdmin />,
    handlers: () => [
      ok("/api/inventory/templates", page([])),
      ok("/api/inventory/devices/dev-1/ports", [PORT]),
      ok("/api/inventory/devices", page([])),
      flaky("/api/cabling/connections", page([]), "cabling is restarting"),
    ],
    alertText: "Could not load this device's connections. cabling is restarting",
    retryName: "Retry loading this device's connections",
  },
];

function renderSpec(spec: SecondarySpec) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  server.use(...spec.handlers());
  const route = spec.route ?? { path: "/", url: "/" };
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route.url]}>
        <Routes>
          <Route path={route.path} element={spec.element()} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  failing = true;
  vi.mocked(toast.error).mockClear();
  useAuthStore.setState({
    user: { id: "1", role: "admin", username: "admin", email: "a@b.c" },
    accessToken: "t",
    isAuthenticated: true,
  } as never);
});

describe.each(SPECS)("$name (issue #1320)", (spec) => {
  it("a failed load shows the load error where it would fill, and Retry clears it", async () => {
    renderSpec(spec);
    if (spec.open) await spec.open();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(spec.alertText);
    if (spec.neverText) {
      expect(screen.queryByText(spec.neverText, { exact: false })).not.toBeInTheDocument();
    }

    failing = false;
    fireEvent.click(screen.getByRole("button", { name: spec.retryName }));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  });
});

// Issue #1320 review: LoadError's Retry is `type="button"`. Inside a <form> a
// default (submit) button would submit the form on Retry; the two forms that
// hold one would then run their submit handler.
describe("Retry inside a form submits nothing (issue #1320)", () => {
  it("Add Device's template Retry does not submit the device form", async () => {
    const posts: string[] = [];
    server.use(
      flaky("/api/inventory/templates", page([]), "inventory is restarting"),
      http.post("/api/inventory/devices", ({ request }) => {
        posts.push(request.url);
        return HttpResponse.json({});
      }),
    );
    renderSpec({
      name: "AddDevicePage",
      element: () => <AddDevicePage />,
      handlers: () => [],
      alertText: "",
      retryName: "",
    });
    fireEvent.click(screen.getByRole("button", { name: "Add Device" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "edge-9" } });
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "Retry loading device templates" }));
    await flushPending();
    expect(vi.mocked(toast.error)).not.toHaveBeenCalled();
    expect(posts).toEqual([]);
  });

  it("the instantiate dialog's device Retry does not submit the dialog", async () => {
    const posts: string[] = [];
    server.use(
      ok(
        "/api/cabling/templates",
        page([
          {
            id: "tt1",
            name: "Two Router Lab",
            description: null,
            created_by: "1",
            owner_name: "admin",
            created_at: STAMP,
            updated_at: STAMP,
          },
        ]),
      ),
      ok("/api/cabling/templates/tt1", INSTANTIATE_TEMPLATE),
      flaky("/api/inventory/devices", page([DEVICE]), "inventory is restarting"),
      http.post("/api/cabling/templates/tt1/instantiate", ({ request }) => {
        posts.push(request.url);
        return HttpResponse.json({ id: "topo-1" });
      }),
    );
    renderSpec({
      name: "TopologyTemplatesPage",
      element: () => <TopologyTemplatesPage />,
      handlers: () => [],
      alertText: "",
      retryName: "",
    });
    fireEvent.click(await screen.findByRole("button", { name: "Use" }));
    fireEvent.change(await screen.findByLabelText("New topology name"), {
      target: { value: "copy" },
    });
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "Retry loading devices" }));
    await flushPending();
    expect(vi.mocked(toast.error)).not.toHaveBeenCalled();
    expect(posts).toEqual([]);
  });
});

describe("a connections page walk that fails part way (issues #1320 and #1336)", () => {
  it("shows the connections load error, never the first page's ports as uncabled", async () => {
    // useDeviceConnections walks every page; a failed second page rejects the
    // whole walk, so the panel shows the failure rather than a partial list.
    const firstPage = Array.from({ length: 500 }, (_, i) => ({
      id: `c${i}`,
      device_a_id: "dev-1",
      port_a: `p${i}`,
      device_b_id: "dev-x",
      port_b: `q${i}`,
      connection_type: "ethernet",
      notes: null,
      created_by: "admin",
      created_at: STAMP,
      modified_by: null,
      updated_at: null,
    }));
    const skips: string[] = [];
    server.use(
      ok("/api/inventory/templates", page([])),
      ok("/api/inventory/devices", page([])),
      ok("/api/inventory/devices/dev-1/ports", [PORT]),
      http.get("/api/cabling/connections", ({ request }) => {
        const skip = new URL(request.url).searchParams.get("skip") ?? "0";
        skips.push(skip);
        if (skip === "0") {
          return HttpResponse.json({ items: firstPage, total: 600, skip: 0, limit: 500 });
        }
        return HttpResponse.json({ detail: "cabling is restarting" }, { status: 500 });
      }),
    );
    renderSpec({
      name: "PortsSection",
      element: () => <PortsSection deviceId="dev-1" isAdmin />,
      handlers: () => [],
      alertText: "",
      retryName: "",
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load this device's connections. cabling is restarting",
    );
    expect(skips).toEqual(["0", "500"]);
  });
});

describe("where a failed connections read shows (issue #1320)", () => {
  it("PortsSection shows only the ports failure when both reads fail", async () => {
    server.use(
      ok("/api/inventory/templates", page([])),
      ok("/api/inventory/devices", page([])),
      flaky("/api/inventory/devices/dev-1/ports", [PORT], "inventory is restarting"),
      flaky("/api/cabling/connections", page([]), "cabling is restarting"),
    );
    renderSpec({
      name: "PortsSection",
      element: () => <PortsSection deviceId="dev-1" isAdmin />,
      handlers: () => [],
      alertText: "",
      retryName: "",
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load this device's ports. inventory is restarting",
    );
    await flushPending();
    expect(screen.getAllByRole("alert")).toHaveLength(1);
  });

  it("PortsSection shows the connections failure above a loaded port table, not in it", async () => {
    server.use(
      ok("/api/inventory/templates", page([])),
      ok("/api/inventory/devices", page([])),
      ok("/api/inventory/devices/dev-1/ports", [PORT]),
      flaky("/api/cabling/connections", page([]), "cabling is restarting"),
    );
    renderSpec({
      name: "PortsSection",
      element: () => <PortsSection deviceId="dev-1" isAdmin />,
      handlers: () => [],
      alertText: "",
      retryName: "",
    });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load this device's connections. cabling is restarting");
    const table = screen.getByRole("table");
    expect(alert.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(table).not.toContainElement(alert);
  });
});
