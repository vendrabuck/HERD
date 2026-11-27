/**
 * Issue #1320: on every list page, a failed fetch is never rendered as "there
 * is nothing here". Each page is driven through its real hooks and MSW, one
 * spec per page, and each spec proves the four states are distinct:
 *
 * - loading: the loading text, before any answer;
 * - error: a network error and a 500 with a detail each render the shared
 *   LoadError (role alert, "Could not load <what>." plus the reason), never
 *   the empty-state text; Retry refetches, and a good answer replaces it;
 * - empty: only a successful empty answer renders the empty-state text;
 * - loaded: a successful answer renders its rows and no alert.
 */
import { http, HttpResponse, type JsonBodyType, type RequestHandler } from "msw";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("react-hot-toast", () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

// The real auth store, so client.ts's request interceptor keeps working.
import { useAuthStore } from "@/stores/authStore";
import { server } from "../mocks/server";
import { DriversPage } from "@/pages/admin/DriversPage";
import { GroupsPage } from "@/pages/admin/GroupsPage";
import { DeviceGroupsPage } from "@/pages/admin/DeviceGroupsPage";
import { HypervisorsPage } from "@/pages/admin/HypervisorsPage";
import { ConnectionsPage } from "@/pages/admin/ConnectionsPage";
import { UsersPage } from "@/pages/admin/UsersPage";
import { LdapSyncPage } from "@/pages/admin/LdapSyncPage";
import { TemplatesPage } from "@/pages/TemplatesPage";
import { TopologyTemplatesPage } from "@/pages/TopologyTemplatesPage";

const STAMP = "2026-01-01T00:00:00Z";

function page(items: unknown[]) {
  return { items, total: items.length, skip: 0, limit: 50 };
}

const ok = (path: string, body: JsonBodyType) => http.get(path, () => HttpResponse.json(body));

type Mode = "pending" | "network" | "server" | "empty" | "loaded";

interface PageSpec {
  name: string;
  element: () => ReactElement;
  /** The page's primary GET, as MSW matches it. */
  endpoint: string;
  what: string;
  loadingText: string;
  emptyText: string;
  emptyBody: JsonBodyType;
  loadedBody: JsonBodyType;
  loadedText: string;
  /** Secondary GETs the page also makes, answered successfully. */
  others: () => RequestHandler[];
}

const SPECS: PageSpec[] = [
  {
    name: "DriversPage",
    element: () => <DriversPage />,
    endpoint: "/api/inventory/drivers",
    what: "drivers",
    loadingText: "Loading drivers...",
    emptyText: "No drivers found",
    emptyBody: page([]),
    loadedBody: page([
      {
        id: "d1",
        name: "Junos Driver",
        description: null,
        connection_type: "Management",
        filename: "junos.zip",
        size_bytes: 1024,
        sha256: "abc",
        uploaded_by: "admin",
        created_at: STAMP,
        updated_at: STAMP,
      },
    ]),
    loadedText: "Junos Driver",
    others: () => [ok("/api/ai/status", { enabled: false })],
  },
  {
    name: "GroupsPage",
    element: () => <GroupsPage />,
    endpoint: "/api/auth/groups",
    what: "user groups",
    loadingText: "Loading groups...",
    emptyText: "No groups found",
    emptyBody: page([]),
    loadedBody: page([
      { id: "g1", name: "Network Team", description: null, created_by: null, created_at: STAMP },
    ]),
    loadedText: "Network Team",
    others: () => [],
  },
  {
    name: "DeviceGroupsPage",
    element: () => <DeviceGroupsPage />,
    endpoint: "/api/inventory/device-groups",
    what: "device groups",
    loadingText: "Loading device groups...",
    emptyText: "No device groups found",
    emptyBody: page([]),
    loadedBody: page([
      {
        id: "dg1",
        name: "Lab Rack",
        description: null,
        device_count: 2,
        user_group_count: 1,
        created_at: STAMP,
      },
    ]),
    loadedText: "Lab Rack",
    others: () => [],
  },
  {
    name: "HypervisorsPage",
    element: () => <HypervisorsPage />,
    endpoint: "/api/inventory/hypervisors",
    what: "hypervisors",
    loadingText: "Loading hypervisors...",
    emptyText: "No hypervisors found",
    emptyBody: page([]),
    loadedBody: page([
      {
        id: "h1",
        name: "pve-01",
        description: null,
        endpoint: "https://pve.example:8006",
        hypervisor_type: "proxmox",
        secret_id: "s1",
        enabled: true,
        device_group_id: null,
        created_at: STAMP,
        updated_at: STAMP,
        modified_by: null,
      },
    ]),
    loadedText: "pve-01",
    others: () => [
      ok("/api/secrets/secrets", []),
      ok("/api/inventory/device-groups", page([])),
    ],
  },
  {
    name: "ConnectionsPage",
    element: () => <ConnectionsPage />,
    endpoint: "/api/cabling/connections",
    what: "connections",
    loadingText: "Loading connections...",
    emptyText: "No connections found",
    emptyBody: page([]),
    loadedBody: page([
      {
        id: "c1",
        device_a_id: "dev-a",
        port_a: "eth0",
        device_b_id: "dev-b",
        port_b: "eth1",
        connection_type: "ethernet",
        notes: "uplink note",
        created_by: "admin",
        created_at: STAMP,
        modified_by: null,
        updated_at: null,
      },
    ]),
    loadedText: "uplink note",
    others: () => [ok("/api/inventory/devices", page([]))],
  },
  {
    name: "UsersPage",
    element: () => <UsersPage />,
    endpoint: "/api/auth/users",
    what: "users",
    loadingText: "Loading users...",
    emptyText: "No users found",
    emptyBody: page([]),
    loadedBody: page([
      {
        id: "u2",
        email: "alice@example.com",
        username: "alice",
        full_name: "Alice Example",
        contact_email: null,
        phone: null,
        is_active: true,
        role: "user",
        created_at: STAMP,
      },
    ]),
    loadedText: "alice@example.com",
    others: () => [],
  },
  {
    name: "LdapSyncPage (mappings)",
    element: () => <LdapSyncPage />,
    endpoint: "/api/auth/admin/ldap-sync/mappings",
    what: "group mappings",
    loadingText: "Loading mappings...",
    emptyText: "No group mappings found",
    emptyBody: page([]),
    loadedBody: page([
      {
        id: "m1",
        group_dn: "cn=herd-eng,ou=groups,dc=example,dc=local",
        directory_name: "herd-eng",
        herd_group_id: "g1",
        created_at: STAMP,
      },
    ]),
    loadedText: "cn=herd-eng,ou=groups,dc=example,dc=local",
    others: () => [
      ok("/api/auth/admin/ldap-sync/status", {
        auth_method: "ldap",
        group_sync_enabled: false,
        sync_interval_seconds: 3600,
      }),
      ok("/api/auth/admin/ldap-sync/runs", page([])),
      ok("/api/auth/groups", page([])),
    ],
  },
  {
    name: "LdapSyncPage (runs)",
    element: () => <LdapSyncPage />,
    endpoint: "/api/auth/admin/ldap-sync/runs",
    what: "sync runs",
    loadingText: "Loading runs...",
    emptyText: "No sync runs found",
    emptyBody: page([]),
    loadedBody: page([
      {
        id: "r1",
        started_at: STAMP,
        finished_at: STAMP,
        trigger: "manual",
        status: "success",
        users_provisioned: 3,
        members_added: 0,
        members_removed: 0,
        members_skipped: 0,
        users_deactivated: 0,
        users_reactivated: 0,
        detail: {},
        error: null,
      },
    ]),
    loadedText: "manual",
    others: () => [
      ok("/api/auth/admin/ldap-sync/status", {
        auth_method: "ldap",
        group_sync_enabled: false,
        sync_interval_seconds: 3600,
      }),
      ok("/api/auth/admin/ldap-sync/mappings", page([])),
      ok("/api/auth/groups", page([])),
    ],
  },
  {
    name: "TemplatesPage",
    element: () => <TemplatesPage />,
    endpoint: "/api/inventory/templates",
    what: "templates",
    loadingText: "Loading templates...",
    emptyText: "No templates found",
    emptyBody: page([]),
    loadedBody: page([
      {
        id: "t1",
        name: "Edge Router",
        template_type: "device",
        driver_id: "drv-1",
        driver_name: "cisco_ios",
        connection_type: "management",
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
      },
    ]),
    loadedText: "Edge Router",
    others: () => [],
  },
  {
    name: "TopologyTemplatesPage",
    element: () => <TopologyTemplatesPage />,
    endpoint: "/api/cabling/templates",
    what: "topology templates",
    loadingText: "Loading templates...",
    emptyText:
      'No templates yet. Open a topology and click "Save as Template" to create one.',
    emptyBody: page([]),
    loadedBody: page([
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
    loadedText: "Two Router Lab",
    others: () => [],
  },
];

let mode: Mode = "loaded";
let requests = 0;

function primaryHandler(spec: PageSpec) {
  return http.get(spec.endpoint, async () => {
    requests += 1;
    switch (mode) {
      case "pending":
        await new Promise(() => {});
        return HttpResponse.json({});
      case "network":
        return HttpResponse.error();
      case "server":
        return HttpResponse.json(
          { detail: spec.name + " backend is down" },
          { status: 500 },
        );
      case "empty":
        return HttpResponse.json(spec.emptyBody);
      case "loaded":
        return HttpResponse.json(spec.loadedBody);
    }
  });
}

function renderSpec(spec: PageSpec) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  // The primary handler goes first so it outranks a catch-all in `others`.
  server.use(primaryHandler(spec), ...spec.others());
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{spec.element()}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mode = "loaded";
  requests = 0;
  useAuthStore.setState({
    user: { id: "1", role: "superadmin", username: "admin", email: "a@b.c" },
    accessToken: "t",
  } as never);
});

describe.each(SPECS)("$name load states (issue #1320)", (spec) => {
  const alertFor = () => screen.findByRole("alert");

  it("shows the loading text, and neither the empty text nor an alert, before an answer", () => {
    mode = "pending";
    renderSpec(spec);
    expect(screen.getByText(spec.loadingText)).toBeInTheDocument();
    expect(screen.queryByText(spec.emptyText)).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("a network error renders the load error, never the empty state", async () => {
    mode = "network";
    renderSpec(spec);
    const alert = await alertFor();
    expect(alert).toHaveTextContent(`Could not load ${spec.what}. The server did not answer.`);
    expect(screen.queryByText(spec.emptyText)).not.toBeInTheDocument();
    expect(screen.queryByText(spec.loadingText)).not.toBeInTheDocument();
  });

  it("a 500 renders the server's detail as text, and Retry loads the rows", async () => {
    mode = "server";
    renderSpec(spec);
    const alert = await alertFor();
    expect(alert).toHaveTextContent(
      `Could not load ${spec.what}. ${spec.name} backend is down`,
    );
    expect(screen.queryByText(spec.emptyText)).not.toBeInTheDocument();
    const before = requests;

    mode = "loaded";
    fireEvent.click(screen.getByRole("button", { name: `Retry loading ${spec.what}` }));

    expect(await screen.findByText(spec.loadedText)).toBeInTheDocument();
    expect(requests).toBe(before + 1);
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(screen.queryByText(spec.emptyText)).not.toBeInTheDocument();
  });

  it("only a successful empty answer renders the empty state", async () => {
    mode = "empty";
    renderSpec(spec);
    expect(await screen.findByText(spec.emptyText)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText(spec.loadingText)).not.toBeInTheDocument();
  });

  it("a successful answer renders its rows, and no empty text or alert", async () => {
    mode = "loaded";
    renderSpec(spec);
    expect(await screen.findByText(spec.loadedText)).toBeInTheDocument();
    expect(screen.queryByText(spec.emptyText)).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
