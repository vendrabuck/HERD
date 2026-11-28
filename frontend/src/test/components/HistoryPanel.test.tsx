/**
 * Issue #1591: the topology History panel lists every version, not only the
 * newest page. It walks the versions route's pages through fetchAllPages (500
 * a page, newest first), so version 1 of a long history can be previewed,
 * compared, and restored; a failed page fails the whole list rather than
 * showing a partial one.
 */
import { http, HttpResponse } from "msw";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi } from "vitest";

import { server } from "../mocks/server";
import { HistoryPanel } from "@/components/topology-editor/HistoryPanel";
import type { TopologyVersion } from "@/types/topology.types";

const TOPO_ID = "topo-1";
const VERSIONS_URL = `/api/cabling/topologies/${TOPO_ID}/versions`;

function version(n: number): TopologyVersion {
  return {
    id: `v-${n}`,
    topology_id: TOPO_ID,
    version_number: n,
    name: "lab",
    description: n === 1 ? "first save" : null,
    created_by: "u1",
    author_name: "alice",
    created_at: "2026-01-01T00:00:00Z",
    restored_from_id: null,
  };
}

/** A history of `count` versions, served newest first in the route's pages. */
function history(count: number, asked: string[], failSkip?: number) {
  const all = Array.from({ length: count }, (_, i) => version(count - i));
  return http.get(VERSIONS_URL, ({ request }) => {
    const url = new URL(request.url);
    const skip = Number(url.searchParams.get("skip"));
    const limit = Number(url.searchParams.get("limit"));
    asked.push(`${skip}:${limit}`);
    if (skip === failSkip) {
      return HttpResponse.json({ detail: "cabling is restarting" }, { status: 503 });
    }
    return HttpResponse.json({ items: all.slice(skip, skip + limit), total: count, skip, limit });
  });
}

function renderPanel() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const props = {
    onClose: vi.fn(),
    onPreview: vi.fn(),
    onRestore: vi.fn(),
    onCompare: vi.fn(),
  };
  render(
    <QueryClientProvider client={client}>
      <HistoryPanel topologyId={TOPO_ID} {...props} />
    </QueryClientProvider>,
  );
  return { ...props, client };
}

const rows = () => screen.getAllByRole("checkbox", { name: /^Select version / });

describe("HistoryPanel lists every version (issue #1591)", () => {
  it("a short history is one request and lists every version", async () => {
    const asked: string[] = [];
    server.use(history(2, asked));
    renderPanel();
    expect(await screen.findByText("v1")).toBeInTheDocument();
    expect(screen.getByText("v2")).toBeInTheDocument();
    expect(rows()).toHaveLength(2);
    expect(asked).toEqual(["0:500"]);
  });

  it("201 versions, past the old 200 cap, list version 1 and version 201", async () => {
    const asked: string[] = [];
    server.use(history(201, asked));
    renderPanel();
    expect(await screen.findByText("v1")).toBeInTheDocument();
    expect(screen.getByText("v201")).toBeInTheDocument();
    expect(rows()).toHaveLength(201);
    expect(asked).toEqual(["0:500"]);
  });

  it("501 versions, past one page, walk two pages and list the oldest and the newest", async () => {
    const asked: string[] = [];
    server.use(history(501, asked));
    renderPanel();
    expect(await screen.findByText("v1")).toBeInTheDocument();
    expect(screen.getByText("v501")).toBeInTheDocument();
    expect(rows()).toHaveLength(501);
    // Newest first, as the route answers, across the page boundary.
    expect(rows()[0]).toHaveAccessibleName("Select version 501");
    expect(rows()[500]).toHaveAccessibleName("Select version 1");
    expect(asked).toEqual(["0:500", "500:500"]);
  });

  it("a failed second page shows the load error, never the first page alone", async () => {
    server.use(history(501, [], 500));
    renderPanel();
    expect(await screen.findByText("Failed to load versions.")).toBeInTheDocument();
    expect(screen.queryByText("v501")).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /^Select version / })).not.toBeInTheDocument();
    expect(screen.queryByText(/^No versions yet\./)).not.toBeInTheDocument();
  });

  it("a failed refresh keeps the rows under a refresh notice, never the load error beside them", async () => {
    server.use(history(2, []));
    const { client } = renderPanel();
    await screen.findByText("v1");

    // The panel is open with rows; a refetch fails (focus, a save, a restore).
    server.use(history(2, [], 0));
    await client.invalidateQueries({ queryKey: ["topologies"] });

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Could not refresh versions. cabling is restarting",
    );
    expect(rows()).toHaveLength(2);
    expect(screen.getByText("v2")).toBeInTheDocument();
    expect(screen.queryByText("Failed to load versions.")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("View, Restore, and Compare reach the oldest version of a long history", async () => {
    server.use(history(501, []));
    const props = renderPanel();
    await screen.findByText("v1");

    // The row that holds version 1's checkbox (its first div ancestor holding a button).
    let oldest = screen.getByRole("checkbox", { name: "Select version 1" }).parentElement as HTMLElement;
    while (!within(oldest).queryByRole("button", { name: "View" })) oldest = oldest.parentElement as HTMLElement;
    fireEvent.click(within(oldest).getByRole("button", { name: "View" }));
    expect(props.onPreview).toHaveBeenCalledWith(expect.objectContaining({ id: "v-1", version_number: 1 }));

    fireEvent.click(within(oldest).getByRole("button", { name: "Restore" }));
    expect(props.onRestore).toHaveBeenCalledWith(expect.objectContaining({ id: "v-1", version_number: 1 }));

    fireEvent.click(screen.getByRole("checkbox", { name: "Select version 1" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select version 501" }));
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    expect(props.onCompare).toHaveBeenCalledWith(
      expect.objectContaining({ id: "v-1" }),
      expect.objectContaining({ id: "v-501" }),
    );
  });
});
