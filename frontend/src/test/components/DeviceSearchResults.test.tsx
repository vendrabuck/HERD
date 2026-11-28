/**
 * Issue #1590: the device typeaheads (DeviceSearchPicker in the multi-connect
 * dialog, the Connections page's filter and single-pair searches) ask for one
 * page of DEVICE_SEARCH_LIMIT matches. More matches than that are never
 * silently missing: MoreMatchesNotice says how many match and that typing
 * narrows the list. A search too short to run sends no request.
 */
import { http, HttpResponse } from "msw";
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
import { DEVICE_SEARCH_LIMIT } from "@/api/inventory";
import { DeviceSearchPicker } from "@/components/admin/connections/DeviceSearchPicker";
import { MoreMatchesNotice } from "@/components/ui/MoreMatchesNotice";
import { ConnectionsPage } from "@/pages/admin/ConnectionsPage";

const NOTICE = /^Matching devices not listed: \d+\. Type more of the name to narrow the list\.$/;

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
      screen.getByText("Matching devices not listed: 15. Type more of the name to narrow the list."),
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
    expect(screen.getByText(/^Matching devices not listed: 1\./)).toBeInTheDocument();
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
      screen.getByText("Matching devices not listed: 15. Type more of the name to narrow the list."),
    ).toBeInTheDocument();
    expect(asked.map((p) => [p.get("search"), p.get("skip"), p.get("limit")])).toEqual([
      ["lab", "0", "20"],
    ]);
  });

  it("while a new term is on its way, the previous rows stay and the notice is hidden", async () => {
    server.use(
      http.get("/api/inventory/devices", ({ request }) => {
        const params = new URL(request.url).searchParams;
        if (params.get("limit") === "500") return HttpResponse.json({ items: [], total: 0, skip: 0, limit: 500 });
        // "lab" answers 20 of 35; "labx" never answers.
        if (params.get("search") !== "lab") return new Promise<never>(() => {});
        const items = Array.from({ length: DEVICE_SEARCH_LIMIT }, (_, i) => device(i));
        return HttpResponse.json({ items, total: 35, skip: 0, limit: DEVICE_SEARCH_LIMIT });
      }),
    );
    renderPicker();
    type("lab");
    expect(await screen.findByText(NOTICE)).toBeInTheDocument();

    type("labx");
    await flushPending();
    // The rows are the previous term's placeholder; the count would be "lab"'s.
    expect(screen.getAllByRole("button", { name: /^lab-node-/ })).toHaveLength(20);
    expect(screen.queryByText(NOTICE)).not.toBeInTheDocument();
  });

  it("says nothing more when the page holds every match", async () => {
    server.use(searchHandler(3, 3, []));
    renderPicker();
    type("lab");
    expect(await screen.findByRole("button", { name: "lab-node-2" })).toBeInTheDocument();
    expect(screen.queryByText(NOTICE)).not.toBeInTheDocument();
  });

  it("a search still on its way shows nothing, never No devices match", async () => {
    const asked: URLSearchParams[] = [];
    server.use(
      http.get("/api/inventory/devices", ({ request }) => {
        asked.push(new URL(request.url).searchParams);
        return new Promise<never>(() => {});
      }),
    );
    renderPicker();
    type("lab");
    await waitFor(() => expect(asked.length).toBeGreaterThan(0));
    await flushPending();
    expect(screen.queryByText("No devices match")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("searches for the trimmed text", async () => {
    const asked: URLSearchParams[] = [];
    server.use(searchHandler(3, 3, asked));
    renderPicker();
    type("  lab  ");
    expect(await screen.findByRole("button", { name: "lab-node-2" })).toBeInTheDocument();
    expect(asked.map((p) => p.get("search"))).toEqual(["lab"]);
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
      screen.getByText("Matching devices not listed: 44. Type more of the name to narrow the list."),
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
      screen.getByText("Matching devices not listed: 1. Type more of the name to narrow the list."),
    ).toBeInTheDocument();
  });
});
