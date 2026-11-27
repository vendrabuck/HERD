/**
 * Issue #1320: a record or form page whose load failed shows the shared
 * LoadError (role alert, fixed wording plus the server's reason, Retry) and
 * no editable form, so nothing can be saved over a record the page never
 * loaded. Retry with a good answer brings the form back filled from it.
 * Driven through the real hooks and MSW.
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

// DevicePage's sub-sections fetch on their own and have their own suites.
vi.mock("@/components/devices/PortsSection", () => ({
  PortsSection: () => <div data-testid="ports-section" />,
}));
vi.mock("@/components/device-config/DeviceConfigSection", () => ({
  DeviceConfigSection: () => <div data-testid="device-config-section" />,
}));
vi.mock("@/components/inventory/DeviceInfoPanel", () => ({
  DeviceInfoPanel: () => <div data-testid="device-info-panel" />,
}));

import { useAuthStore } from "@/stores/authStore";
import { useConfigStore } from "@/stores/configStore";
import { server } from "../mocks/server";
import { flushPending } from "../flushPending";
import { GroupDetailPage } from "@/pages/admin/GroupDetailPage";
import { DeviceGroupDetailPage } from "@/pages/admin/DeviceGroupDetailPage";
import { TemplateEditorPage } from "@/pages/TemplateEditorPage";
import { DevicePage } from "@/pages/DevicePage";
import { SettingsPage } from "@/pages/SettingsPage";
import { ConfigPage } from "@/pages/ConfigPage";

const STAMP = "2026-01-01T00:00:00Z";
const page = (items: unknown[]) => ({ items, total: items.length, skip: 0, limit: 50 });
const ok = (path: string, body: JsonBodyType) => http.get(path, () => HttpResponse.json(body));

type Failure = "network" | "server";
let failure: Failure | null = "server";
let writes: string[] = [];

/** A GET that fails per `failure` until it is cleared, then answers `body`. */
function failing(path: string, body: JsonBodyType, detail: string): RequestHandler {
  return http.get(path, () => {
    if (failure === "network") return HttpResponse.error();
    if (failure === "server") return HttpResponse.json({ detail }, { status: 500 });
    return HttpResponse.json(body);
  });
}

/** Records every write, so a test can prove none was sent. */
function recordWrites(): RequestHandler[] {
  const record = ({ request }: { request: Request }) => {
    writes.push(request.method + " " + new URL(request.url).pathname);
    return HttpResponse.json({});
  };
  return [http.post("/api/*", record), http.put("/api/*", record), http.patch("/api/*", record), http.delete("/api/*", record)];
}

function renderAt(path: string, url: string, element: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[url]}>
        <Routes>
          <Route path={path} element={element} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  failure = "server";
  writes = [];
  useAuthStore.setState({
    user: { id: "1", role: "admin", username: "admin", email: "a@b.c" },
    accessToken: "t",
    isAuthenticated: true,
  } as never);
  server.use(...recordWrites());
});

const GROUP = {
  id: "g1",
  name: "Network Team",
  description: "routers",
  created_by: null,
  created_at: STAMP,
  members: [],
};

const DEVICE_GROUP = {
  id: "dg1",
  name: "Lab Rack",
  description: null,
  device_count: 0,
  user_group_count: 0,
  created_at: STAMP,
  devices: [],
  user_groups: [],
};

const TEMPLATE = {
  id: "t1",
  name: "Edge Router",
  template_type: "device",
  driver_id: "drv-1",
  driver_name: "cisco_ios",
  connection_type: "Management",
  hypervisor_id: null,
  exclusive: true,
  icon: null,
  description: null,
  vendor: "Cisco",
  model: "ISR4451",
  part_number: null,
  sections: [],
  created_at: STAMP,
  updated_at: STAMP,
  poll_interval_seconds: null,
};

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

describe("GroupDetailPage load failure (issue #1320)", () => {
  function renderGroup() {
    server.use(
      failing("/api/auth/groups/g1", GROUP, "auth is restarting"),
      ok("/api/auth/users", page([])),
    );
    return renderAt("/admin/groups/:id", "/admin/groups/g1", <GroupDetailPage />);
  }

  it.each(["network", "server"] as const)(
    "a %s failure shows the load error and no form, Save, or name field",
    async (kind) => {
      failure = kind;
      renderGroup();
      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent(
        kind === "network"
          ? "Could not load this user group. The server did not answer."
          : "Could not load this user group. auth is restarting",
      );
      expect(screen.queryByLabelText("Name")).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Back to User Groups" })).toBeInTheDocument();
      await flushPending();
      expect(writes).toEqual([]);
    },
  );

  it("Retry loads the group and the form is filled from it", async () => {
    renderGroup();
    await screen.findByRole("alert");
    failure = null;
    fireEvent.click(screen.getByRole("button", { name: "Retry loading this user group" }));
    expect(await screen.findByDisplayValue("Network Team")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("a failed user list shows its own load error above the member picker", async () => {
    failure = null;
    server.use(
      ok("/api/auth/groups/g1", GROUP),
      http.get("/api/auth/users", () =>
        HttpResponse.json({ detail: "directory offline" }, { status: 503 }),
      ),
    );
    renderAt("/admin/groups/:id", "/admin/groups/g1", <GroupDetailPage />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load users. directory offline");
    // The group itself loaded, so its form stays.
    expect(screen.getByDisplayValue("Network Team")).toBeInTheDocument();
  });
});

describe("DeviceGroupDetailPage load failure (issue #1320)", () => {
  function renderDeviceGroup() {
    server.use(
      failing("/api/inventory/device-groups/dg1", DEVICE_GROUP, "inventory is restarting"),
      ok("/api/inventory/devices", page([])),
      ok("/api/auth/groups", page([])),
    );
    return renderAt(
      "/admin/device-groups/:id",
      "/admin/device-groups/dg1",
      <DeviceGroupDetailPage />,
    );
  }

  it.each(["network", "server"] as const)(
    "a %s failure shows the load error and no form, Save, or Delete",
    async (kind) => {
      failure = kind;
      renderDeviceGroup();
      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent(
        kind === "network"
          ? "Could not load this device group. The server did not answer."
          : "Could not load this device group. inventory is restarting",
      );
      expect(screen.queryByLabelText("Name")).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Delete Device Group" }),
      ).not.toBeInTheDocument();
      await flushPending();
      expect(writes).toEqual([]);
    },
  );

  it("Retry loads the device group and the form is filled from it", async () => {
    renderDeviceGroup();
    await screen.findByRole("alert");
    failure = null;
    fireEvent.click(screen.getByRole("button", { name: "Retry loading this device group" }));
    expect(await screen.findByDisplayValue("Lab Rack")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete Device Group" })).toBeInTheDocument();
  });
});

describe("TemplateEditorPage load failure (issue #1320)", () => {
  function renderTemplate() {
    server.use(
      failing("/api/inventory/templates/t1", TEMPLATE, "inventory is restarting"),
      ok("/api/inventory/drivers", page([])),
      ok("/api/inventory/hypervisors", page([])),
      ok("/api/ai/status", { enabled: false }),
    );
    return renderAt("/templates/:id", "/templates/t1", <TemplateEditorPage />);
  }

  it.each(["network", "server"] as const)(
    "a %s failure shows the load error, not 'Template not found', and no editor",
    async (kind) => {
      failure = kind;
      renderTemplate();
      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent(
        kind === "network"
          ? "Could not load this template. The server did not answer."
          : "Could not load this template. inventory is restarting",
      );
      expect(screen.queryByText("Template not found")).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
    },
  );

  it("a 404 keeps the deliberate 'Template not found'", async () => {
    server.use(
      http.get("/api/inventory/templates/t1", () =>
        HttpResponse.json({ detail: "Template not found" }, { status: 404 }),
      ),
      ok("/api/inventory/drivers", page([])),
      ok("/api/inventory/hypervisors", page([])),
      ok("/api/ai/status", { enabled: false }),
    );
    renderAt("/templates/:id", "/templates/t1", <TemplateEditorPage />);
    expect(await screen.findByText("Template not found")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("Retry loads the template", async () => {
    renderTemplate();
    await screen.findByRole("alert");
    failure = null;
    fireEvent.click(screen.getByRole("button", { name: "Retry loading this template" }));
    expect(await screen.findByText("Template Details")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("a failed driver list shows its own load error under the driver picker", async () => {
    failure = null;
    server.use(
      ok("/api/inventory/templates/t1", TEMPLATE),
      http.get("/api/inventory/drivers", () => HttpResponse.error()),
      ok("/api/inventory/hypervisors", page([])),
      ok("/api/ai/status", { enabled: false }),
    );
    renderAt("/templates/:id", "/templates/t1", <TemplateEditorPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load drivers. The server did not answer.",
    );
  });
});

describe("DevicePage load failure (issue #1320)", () => {
  function renderDevice() {
    server.use(failing("/api/inventory/devices/dev-1", DEVICE, "inventory is restarting"));
    return renderAt("/inventory/:id", "/inventory/dev-1", <DevicePage />);
  }

  it.each(["network", "server"] as const)(
    "a %s failure shows the load error, not 'Device not found', and no Edit",
    async (kind) => {
      failure = kind;
      renderDevice();
      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent(
        kind === "network"
          ? "Could not load this device. The server did not answer."
          : "Could not load this device. inventory is restarting",
      );
      expect(screen.queryByText("Device not found")).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
      expect(screen.queryByTestId("ports-section")).not.toBeInTheDocument();
    },
  );

  it("Retry loads the device", async () => {
    renderDevice();
    await screen.findByRole("alert");
    failure = null;
    fireEvent.click(screen.getByRole("button", { name: "Retry loading this device" }));
    expect(await screen.findByText("core-fw-01")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("a failed template load says so where the device's fields would be", async () => {
    failure = null;
    server.use(
      ok("/api/inventory/devices/dev-1", { ...DEVICE, template_id: "t1", template_name: "Edge Router" }),
      http.get("/api/inventory/templates/t1", () =>
        HttpResponse.json({ detail: "template store offline" }, { status: 500 }),
      ),
    );
    renderAt("/inventory/:id", "/inventory/dev-1", <DevicePage />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load this device's template. template store offline",
    );
  });
});

describe("SettingsPage load failure (issue #1320)", () => {
  const PREFS = { channels: { in_app: true, email: true }, events: {} };
  const ME = {
    id: "1",
    email: "admin@example.com",
    username: "admin",
    full_name: "Admin",
    contact_email: null,
    phone: null,
    is_active: true,
    role: "admin",
    created_at: STAMP,
    updated_at: STAMP,
  };

  it.each(["network", "server"] as const)(
    "a %s failure of the preferences shows the load error and no checkboxes or Save",
    async (kind) => {
      failure = kind;
      server.use(
        failing("/api/notifications/notifications/preferences", PREFS, "notifications is down"),
        ok("/api/auth/me", ME),
      );
      renderAt("/settings", "/settings", <SettingsPage />);
      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent(
        kind === "network"
          ? "Could not load your notification preferences. The server did not answer."
          : "Could not load your notification preferences. notifications is down",
      );
      expect(screen.queryByLabelText("Show in-app notifications")).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
      // The profile is a separate load and still renders.
      expect(await screen.findByRole("button", { name: "Save profile" })).toBeInTheDocument();
      await flushPending();
      expect(writes).toEqual([]);
    },
  );

  it("Retry loads the stored preferences, not the defaults", async () => {
    server.use(
      failing("/api/notifications/notifications/preferences", PREFS, "notifications is down"),
      ok("/api/auth/me", ME),
    );
    renderAt("/settings", "/settings", <SettingsPage />);
    await screen.findByRole("alert");
    failure = null;
    fireEvent.click(
      screen.getByRole("button", { name: "Retry loading your notification preferences" }),
    );
    const email = await screen.findByRole("checkbox", { name: "Email" });
    // The stored choice (email on) shows, where the defaults have it off.
    expect(email).toBeChecked();
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
  });

  it("a failed profile load shows its load error instead of loading forever", async () => {
    failure = null;
    server.use(
      ok("/api/notifications/notifications/preferences", PREFS),
      http.get("/api/auth/me", () => HttpResponse.json({ detail: "auth is down" }, { status: 503 })),
    );
    renderAt("/settings", "/settings", <SettingsPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load your profile. auth is down",
    );
    expect(screen.queryByText("Loading profile...")).not.toBeInTheDocument();
  });
});

describe("ConfigPage load failure (issue #1320)", () => {
  const SCHEMA = {
    fields: [
      {
        key: "LOG_LEVEL",
        label: "Log level",
        type: "string",
        required: false,
        group: "General",
        secret: false,
        description: "",
        default: "INFO",
      },
    ],
  };
  const SETTINGS = { values: { LOG_LEVEL: "DEBUG" } };

  beforeEach(() => {
    useConfigStore.getState().setConfigToken("ct");
    server.use(ok("/api/config/status", { configured: true, password_changed: true }));
  });

  it.each([
    ["the saved configuration", "settings"],
    ["the configuration schema", "schema"],
  ] as const)("a failed %s load shows the load error and no Save", async (what, which) => {
    server.use(
      which === "settings"
        ? failing("/api/config/settings", SETTINGS, "config store unreadable")
        : ok("/api/config/settings", SETTINGS),
      which === "schema"
        ? failing("/api/config/schema", SCHEMA, "config store unreadable")
        : ok("/api/config/schema", SCHEMA),
    );
    renderAt("/config", "/config", <ConfigPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      `Could not load ${what}. config store unreadable`,
    );
    expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save and Restart" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
    await flushPending();
    expect(writes).toEqual([]);
  });

  it("Retry loads the saved values, not the schema defaults", async () => {
    server.use(
      failing("/api/config/settings", SETTINGS, "config store unreadable"),
      ok("/api/config/schema", SCHEMA),
    );
    renderAt("/config", "/config", <ConfigPage />);
    await screen.findByRole("alert");
    failure = null;
    fireEvent.click(
      screen.getByRole("button", { name: "Retry loading the saved configuration" }),
    );
    expect(await screen.findByDisplayValue("DEBUG")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Save and Restart" })).toBeInTheDocument();
  });
});
