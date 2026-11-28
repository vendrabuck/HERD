/**
 * Issue #1590: the device typeaheads (DeviceSearchPicker in the multi-connect
 * dialog, the Connections page's filter and single-pair searches) ask for one
 * page of DEVICE_SEARCH_LIMIT matches. More matches than that are never
 * silently missing: MoreMatchesNotice says how many match and that typing
 * narrows the list. A search too short to run sends no request.
 */
import { http, HttpResponse } from "msw";
import { render, screen, fireEvent } from "@testing-library/react";
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
import { DEVICE_SEARCH_LIMIT } from "@/api/inventory";
import { DeviceSearchPicker } from "@/components/admin/connections/DeviceSearchPicker";
import { MoreMatchesNotice } from "@/components/ui/MoreMatchesNotice";
import { ConnectionsPage } from "@/pages/admin/ConnectionsPage";

const NOTICE = /^\d+ more matching devices are not listed\. Type more of the name to narrow the list\.$/;

function device(i: number) {
  return { id: `dev-${i}`, name: `lab-node-${i}`, topology_type: "PHYSICAL" };
}

/** Search requests (any limit but the all-names walk's 500), answered with `shown` rows of `total`. */
function searchHandler(shown: number, total: number, asked: URLSearchParams[]) {
  return http.get("/api/inventory/devices", ({ request }) => {
    const params = new URL(request.url).searchParams;
    if (params.get("limit") === "500") return HttpResponse.json({ items: [], total: 0, skip: 0, limit: 500 });
    asked.push(params);
    const items = Array.from({ length: shown }, (_, i) => device(i));
    return HttpResponse.json({ items, total, skip: 0, limit: Number(params.get("limit")) });
  });
}

function renderWith(element: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{element}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  useAuthStore.setState({
    user: { id: "1", role: "admin", username: "admin", email: "a@b.c" },
    accessToken: "t",
    isAuthenticated: true,
  } as never);
});

describe("MoreMatchesNotice (issue #1590)", () => {
  it("names how many matches are past the page, and the way to them", () => {
    render(<MoreMatchesNotice shown={20} total={35} noun="devices" />);
    expect(
      screen.getByText("15 more matching devices are not listed. Type more of the name to narrow the list."),
    ).toBeInTheDocument();
  });

  it.each([
    ["every match shown", 20, 20],
    ["fewer matches than shown (a total that lags)", 20, 19],
    ["no total", 20, undefined],
    ["a total that is not a number", 20, "35"],
    ["an infinite total", 20, Infinity],
  ])("renders nothing for %s", (_label, shown, total) => {
    const { container } = render(<MoreMatchesNotice shown={shown} total={total} noun="devices" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("speaks of the first match past the page", () => {
    render(<MoreMatchesNotice shown={20} total={21} noun="devices" />);
    expect(screen.getByText(/^1 more matching devices are not listed\./)).toBeInTheDocument();
  });
});

describe("DeviceSearchPicker, more matches than one page (issue #1590)", () => {
  const renderPicker = () =>
    renderWith(<DeviceSearchPicker label="Device A" device={null} onSelect={() => {}} onClear={() => {}} />);
  const type = (value: string) =>
    fireEvent.change(screen.getByRole("textbox", { name: "Search Device A" }), { target: { value } });

  it("asks for one page of DEVICE_SEARCH_LIMIT and says how many more match", async () => {
    const asked: URLSearchParams[] = [];
    server.use(searchHandler(DEVICE_SEARCH_LIMIT, 35, asked));
    renderPicker();
    type("lab");

    expect(await screen.findByRole("button", { name: "lab-node-19" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /^lab-node-/ })).toHaveLength(20);
    expect(
      screen.getByText("15 more matching devices are not listed. Type more of the name to narrow the list."),
    ).toBeInTheDocument();
    expect(asked.map((p) => [p.get("search"), p.get("skip"), p.get("limit")])).toEqual([
      ["lab", "0", "20"],
    ]);
  });

  it("says nothing more when the page holds every match", async () => {
    server.use(searchHandler(3, 3, []));
    renderPicker();
    type("lab");
    expect(await screen.findByRole("button", { name: "lab-node-2" })).toBeInTheDocument();
    expect(screen.queryByText(NOTICE)).not.toBeInTheDocument();
  });

  it("a one-character search sends no request and shows nothing", async () => {
    const asked: URLSearchParams[] = [];
    server.use(searchHandler(3, 3, asked));
    renderPicker();
    type("l");
    await flushPending();
    expect(asked).toEqual([]);
    expect(screen.queryByRole("button", { name: /^lab-node-/ })).not.toBeInTheDocument();
    expect(screen.queryByText("No devices match")).not.toBeInTheDocument();
  });
});

describe("ConnectionsPage typeaheads, more matches than one page (issue #1590)", () => {
  it("the device filter says how many more match", async () => {
    const asked: URLSearchParams[] = [];
    server.use(
      http.get("/api/cabling/connections", () => HttpResponse.json({ items: [], total: 0, skip: 0, limit: 50 })),
      searchHandler(DEVICE_SEARCH_LIMIT, 64, asked),
    );
    renderWith(<ConnectionsPage />);
    fireEvent.change(screen.getByPlaceholderText("Filter by device name..."), { target: { value: "l" } });

    expect(await screen.findByRole("button", { name: "lab-node-19" })).toBeInTheDocument();
    expect(
      screen.getByText("44 more matching devices are not listed. Type more of the name to narrow the list."),
    ).toBeInTheDocument();
    expect(asked.map((p) => [p.get("search"), p.get("limit")])).toEqual([["l", "20"]]);
  });

  it.each([0, 1])("the single-pair search %i says how many more match", async (index) => {
    server.use(
      http.get("/api/cabling/connections", () => HttpResponse.json({ items: [], total: 0, skip: 0, limit: 50 })),
      searchHandler(DEVICE_SEARCH_LIMIT, 21, []),
    );
    renderWith(<ConnectionsPage />);
    fireEvent.click(screen.getByRole("button", { name: "Single" }));
    fireEvent.click(screen.getByRole("button", { name: "Create Connection" }));
    fireEvent.change(screen.getAllByPlaceholderText("Search devices...")[index], { target: { value: "lab" } });

    expect(await screen.findByRole("button", { name: "lab-node-19" })).toBeInTheDocument();
    expect(
      screen.getByText("1 more matching devices are not listed. Type more of the name to narrow the list."),
    ).toBeInTheDocument();
  });
});
