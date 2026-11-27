import { http, HttpResponse } from "msw";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, it, expect } from "vitest";

import { server } from "../mocks/server";
import {
  useReservationFork,
  useSaveReservationFork,
  putForkCanvas,
  saveReservationFork,
  forkConflictDetail,
  forkDeviceNotMemberDetail,
  forkL3IntentInvalidDetail,
  forkL3IntentMalformedDetail,
  topologyRoutingIntentInvalidDetail,
} from "@/api/reservations";
import type { CanvasData } from "@/types/topology.types";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const RES_ID = "11111111-1111-1111-1111-111111111111";

const FORK = {
  id: "f0000000-0000-0000-0000-000000000001",
  reservation_id: RES_ID,
  parent_topology_id: "topo-1",
  parent_version_id: null,
  status: "ACTIVE",
  canvas_data: { nodes: [], edges: [], selectedEdgeLayer: "L2" },
  created_at: "2026-06-01T00:00:00Z",
  updated_at: "2026-06-01T00:00:00Z",
  connections: [],
  versions: [
    {
      id: "v-1",
      fork_id: "f0000000-0000-0000-0000-000000000001",
      version_number: 1,
      restored_from_id: null,
      created_at: "2026-06-01T00:00:00Z",
    },
  ],
};

const EMPTY_CANVAS: CanvasData = { nodes: [], edges: [], selectedEdgeLayer: "L2" };

describe("reservation fork api", () => {
  it("useReservationFork GETs /reservations/{id}/fork and returns the fork", async () => {
    server.use(http.get(`/api/reservations/${RES_ID}/fork`, () => HttpResponse.json(FORK)));
    const { result } = renderHook(() => useReservationFork(RES_ID), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.status).toBe("ACTIVE");
    expect(result.current.data?.versions).toHaveLength(1);
  });

  it("useReservationFork stays disabled with no reservation id", () => {
    const { result } = renderHook(() => useReservationFork(null), { wrapper });
    expect(result.current.fetchStatus).toBe("idle");
  });

  it("putForkCanvas PUTs the loose draft to the canvas endpoint", async () => {
    let method = "";
    let body: unknown;
    server.use(
      http.put(`/api/reservations/${RES_ID}/fork/canvas`, async ({ request }) => {
        method = request.method;
        body = await request.json();
        return HttpResponse.json({ id: FORK.id, valid: true, invalid_edges: [] });
      }),
    );
    const result = await putForkCanvas(RES_ID, EMPTY_CANVAS);
    expect(method).toBe("PUT");
    expect(body).toEqual({ canvas_data: EMPTY_CANVAS });
    expect(result.valid).toBe(true);
  });

  it("saveReservationFork POSTs to the save endpoint and returns the delta", async () => {
    server.use(
      http.post(`/api/reservations/${RES_ID}/fork/save`, () =>
        HttpResponse.json({
          fork_id: FORK.id,
          version_number: 2,
          released: [
            { device_a_id: "d-a", port_a: "1", device_b_id: "d-b", port_b: "2", layer: "L2" },
          ],
          built: [
            { device_a_id: "d-a", port_a: "1", device_b_id: "d-c", port_b: "3", layer: "L2" },
          ],
          unchanged_count: 4,
        }),
      ),
    );
    const result = await saveReservationFork(RES_ID, EMPTY_CANVAS);
    expect(result.version_number).toBe(2);
    expect(result.released).toHaveLength(1);
    expect(result.built).toHaveLength(1);
    expect(result.unchanged_count).toBe(4);
  });

  it("useSaveReservationFork mutation resolves with the save result", async () => {
    server.use(
      http.post(`/api/reservations/${RES_ID}/fork/save`, () =>
        HttpResponse.json({
          fork_id: FORK.id,
          version_number: 3,
          released: [],
          built: [],
          unchanged_count: 0,
        }),
      ),
    );
    const { result } = renderHook(() => useSaveReservationFork(), { wrapper });
    result.current.mutate({ reservationId: RES_ID, canvasData: EMPTY_CANVAS });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.version_number).toBe(3);
  });
});

describe("forkConflictDetail", () => {
  it("extracts a structured port-claim 409 detail", () => {
    const err = {
      response: {
        status: 409,
        data: {
          detail: {
            message: "One or more ports are already claimed by another active reservation",
            conflicts: [{ reservation_id: "r-2", device_id: "d-9", port: "eth1" }],
          },
        },
      },
    };
    const detail = forkConflictDetail(err);
    expect(detail).not.toBeNull();
    expect(detail?.conflicts[0]).toEqual({
      reservation_id: "r-2",
      device_id: "d-9",
      port: "eth1",
    });
  });

  it("returns null for a plain-string 409 (non-active / archived refusal)", () => {
    const err = {
      response: { status: 409, data: { detail: "Fork save requires an ACTIVE reservation" } },
    };
    expect(forkConflictDetail(err)).toBeNull();
  });

  it("returns null for a non-409 error", () => {
    expect(forkConflictDetail({ response: { status: 503, data: { detail: "down" } } })).toBeNull();
    expect(forkConflictDetail(new Error("boom"))).toBeNull();
  });
});

// Issue #1406: the conflict dialog renders `message` and every conflict's
// `reservation_id`, `device_id`, and `port`, so the narrower hands it a body
// only when each of those is a string (`message` may also be absent). Every
// other shape answers null and the editor shows its plain toast instead.
describe("forkConflictDetail checks every field the dialog renders (issue #1406)", () => {
  const GOOD = { reservation_id: "r-2", device_id: "d-9", port: "eth1" };
  const HONEST = { message: "Ports already claimed", conflicts: [GOOD] };
  const err409 = (detail: unknown) => ({ response: { status: 409, data: { detail } } });

  it.each([
    ["the honest body", HONEST],
    ["a body with no message", { conflicts: [GOOD] }],
    ["an empty conflict list", { message: "m", conflicts: [] }],
    ["several conflicts", { message: "m", conflicts: [GOOD, { ...GOOD, port: "eth2" }] }],
    ["extra fields on the body and on an entry", { ...HONEST, extra: 1, conflicts: [{ ...GOOD, x: {} }] }],
  ])("accepts %s", (_name, detail) => {
    expect(forkConflictDetail(err409(detail))).toEqual(detail);
  });

  it.each([
    ["an object message", { message: {}, conflicts: [GOOD] }],
    ["a list message", { message: ["a"], conflicts: [GOOD] }],
    ["a numeric message", { message: 5, conflicts: [GOOD] }],
    ["a null message", { message: null, conflicts: [GOOD] }],
    ["a boolean message", { message: true, conflicts: [GOOD] }],
    ["no conflicts", { message: "m" }],
    ["null conflicts", { message: "m", conflicts: null }],
    ["object conflicts", { message: "m", conflicts: { 0: GOOD } }],
    ["string conflicts", { message: "m", conflicts: "r-2 d-9 eth1" }],
    ["a null entry", { message: "m", conflicts: [null] }],
    ["a string entry", { message: "m", conflicts: ["r-2"] }],
    ["a list entry", { message: "m", conflicts: [["r-2", "d-9", "eth1"]] }],
    ["a numeric entry", { message: "m", conflicts: [5] }],
    ["one bad entry after a good one", { message: "m", conflicts: [GOOD, { ...GOOD, port: 1 }] }],
    ["a numeric device_id", { message: "m", conflicts: [{ ...GOOD, device_id: 9 }] }],
    ["a null device_id", { message: "m", conflicts: [{ ...GOOD, device_id: null }] }],
    ["an object device_id", { message: "m", conflicts: [{ ...GOOD, device_id: {} }] }],
    ["a missing device_id", { message: "m", conflicts: [{ reservation_id: "r-2", port: "eth1" }] }],
    ["a numeric reservation_id", { message: "m", conflicts: [{ ...GOOD, reservation_id: 2 }] }],
    ["a null reservation_id", { message: "m", conflicts: [{ ...GOOD, reservation_id: null }] }],
    ["a missing reservation_id", { message: "m", conflicts: [{ device_id: "d-9", port: "eth1" }] }],
    ["a numeric port", { message: "m", conflicts: [{ ...GOOD, port: 1 }] }],
    ["an object port", { message: "m", conflicts: [{ ...GOOD, port: { name: "eth1" } }] }],
    ["a missing port", { message: "m", conflicts: [{ reservation_id: "r-2", device_id: "d-9" }] }],
  ])("refuses %s", (_name, detail) => {
    expect(forkConflictDetail(err409(detail))).toBeNull();
  });

  it.each([
    ["a null detail", err409(null)],
    ["a list detail", err409([HONEST])],
    ["a string detail", err409("Ports already claimed")],
    ["an HTML string detail", err409("<html><body>409</body></html>")],
    ["a numeric detail", err409(409)],
    ["an HTML page as the whole body", { response: { status: 409, data: "<html>409</html>" } }],
    ["no body at all", { response: { status: 409 } }],
    ["the honest body on a 422", { response: { status: 422, data: { detail: HONEST } } }],
  ])("refuses %s", (_name, err) => {
    expect(forkConflictDetail(err)).toBeNull();
  });
});

// Issue #1406, one hop upstream: every other fork narrower the editor reads
// from checks the fields it hands to a toast or to the Routing panel.
describe("the other fork narrowers check what they hand on (issue #1406)", () => {
  const errOf = (status: number, detail: unknown) => ({ response: { status, data: { detail } } });
  const ROUTE = { node_id: "n-1", device_id: "d-1", index: 0, reason: "l3_bad_destination", detail: null };

  it("forkDeviceNotMemberDetail takes a list of string ids only", () => {
    const ok = { error: "fork_device_not_member", device_ids: ["d-x", "d-y"] };
    expect(forkDeviceNotMemberDetail(errOf(409, ok))).toEqual(ok);
    expect(forkDeviceNotMemberDetail(errOf(409, { ...ok, device_ids: [] }))).not.toBeNull();
    for (const device_ids of [["d-x", 7], [null], [{}], "d-x", null, undefined]) {
      expect(forkDeviceNotMemberDetail(errOf(409, { ...ok, device_ids }))).toBeNull();
    }
  });

  it("forkL3IntentInvalidDetail takes only well-formed route entries", () => {
    const ok = { error: "l3_intent_invalid", invalid_routes: [ROUTE] };
    expect(forkL3IntentInvalidDetail(errOf(409, ok))).toEqual(ok);
    const switchLevel = { ...ROUTE, device_id: null, index: null, reason: "l3_malformed", detail: "bad" };
    expect(forkL3IntentInvalidDetail(errOf(409, { ...ok, invalid_routes: [switchLevel] }))).not.toBeNull();
    const broken = [
      null,
      "l3_bad_destination",
      { ...ROUTE, reason: {} },
      { ...ROUTE, reason: null },
      { ...ROUTE, detail: {} },
      { ...ROUTE, detail: 3 },
      { ...ROUTE, node_id: 1 },
      { ...ROUTE, device_id: 1 },
      { ...ROUTE, index: "0" },
      { ...ROUTE, index: 0.5 },
      { node_id: "n-1", reason: "l3_bad_destination" },
    ];
    for (const entry of broken) {
      expect(forkL3IntentInvalidDetail(errOf(409, { ...ok, invalid_routes: [ROUTE, entry] }))).toBeNull();
    }
  });

  it("forkL3IntentMalformedDetail takes a string node_id and message", () => {
    const ok = { error: "l3_intent_malformed", node_id: "n-1", message: "'l3' must be an object" };
    expect(forkL3IntentMalformedDetail(errOf(422, ok))).toEqual(ok);
    for (const message of [{}, ["a"], 1, null, undefined]) {
      expect(forkL3IntentMalformedDetail(errOf(422, { ...ok, message }))).toBeNull();
    }
    expect(forkL3IntentMalformedDetail(errOf(422, { ...ok, node_id: 1 }))).toBeNull();
  });

  it("topologyRoutingIntentInvalidDetail takes a string message and well-formed routes", () => {
    const ok = { error: "topology_routing_intent_invalid", invalid_routes: [ROUTE], message: "m" };
    expect(topologyRoutingIntentInvalidDetail(errOf(422, ok))).toEqual(ok);
    expect(topologyRoutingIntentInvalidDetail(errOf(422, { ...ok, message: {} }))).toBeNull();
    expect(
      topologyRoutingIntentInvalidDetail(errOf(422, { ...ok, invalid_routes: [{ ...ROUTE, reason: [] }] })),
    ).toBeNull();
    expect(topologyRoutingIntentInvalidDetail(errOf(422, { ...ok, invalid_routes: null }))).toBeNull();
  });
});
