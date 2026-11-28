import { http, HttpResponse } from "msw";
import { AxiosError, type AxiosResponse, type InternalAxiosRequestConfig } from "axios";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { server } from "../mocks/server";
import apiClient from "@/api/client";
import {
  isUnreadableAnswer,
  rejectUnreadableAnswer,
  UNREADABLE_ANSWER_CODE,
  UNREADABLE_ANSWER_MESSAGE,
} from "@/api/answerGuard";
import { exportDevices } from "@/api/bulk";
import { downloadUtilizationCsv } from "@/api/reporting";
import { fetchServiceVersion, InvalidVersionResponseError } from "@/api/about";
import { useConfigSchema } from "@/api/config";
import { loadFailureDetail, LOAD_FAILURE_UNREADABLE } from "@/lib/errors";

const SPA_PAGE = '<!doctype html><html><body><div id="root"></div></body></html>';

function response(
  data: unknown,
  contentType: string | undefined,
  options: { status?: number; responseType?: InternalAxiosRequestConfig["responseType"] } = {},
): AxiosResponse {
  const config = { headers: {}, responseType: options.responseType } as InternalAxiosRequestConfig;
  return {
    data,
    status: options.status ?? 200,
    statusText: "OK",
    headers: contentType === undefined ? {} : { "content-type": contentType },
    config,
    request: {},
  } as AxiosResponse;
}

function refused(resp: AxiosResponse): unknown {
  try {
    rejectUnreadableAnswer(resp);
  } catch (err) {
    return err;
  }
  return null;
}

describe("rejectUnreadableAnswer (issue #1320)", () => {
  // The space: content type x body shape x what the request asked for.
  const refusedCases: Array<[string, AxiosResponse]> = [
    ["an HTML page", response(SPA_PAGE, "text/html")],
    ["an HTML page with a charset", response(SPA_PAGE, "text/html; charset=utf-8")],
    ["an HTML content type with an empty body", response("", "text/html")],
    ["HTML in upper case", response(SPA_PAGE, "TEXT/HTML")],
    ["plain text that did not parse", response("Bad Gateway", "text/plain")],
    ["text with no content type", response("<html></html>", undefined)],
    ["an HTML page answering an explicit json request", response(SPA_PAGE, "text/html", { responseType: "json" })],
  ];
  it.each(refusedCases)("refuses %s", (_label, resp) => {
    const err = refused(resp);
    expect(err).toBeInstanceOf(AxiosError);
    expect(isUnreadableAnswer(err)).toBe(true);
    expect((err as AxiosError).code).toBe(UNREADABLE_ANSWER_CODE);
    expect((err as AxiosError).message).toBe(UNREADABLE_ANSWER_MESSAGE);
    // No response is attached: no 401 refresh, no body text in the UI.
    expect((err as AxiosError).response).toBeUndefined();
    expect(loadFailureDetail(err)).toBe(LOAD_FAILURE_UNREADABLE);
  });

  const passedCases: Array<[string, AxiosResponse]> = [
    ["a JSON object", response({ items: [] }, "application/json")],
    ["a JSON list", response([], "application/json")],
    ["a JSON string answer", response("ok", "application/json")],
    ["a problem+json body", response({ detail: "x" }, "application/problem+json")],
    ["a 204", response("", undefined, { status: 204 })],
    // A 204 carries no body to read, whatever content type a proxy stamps on it.
    ["a 204 stamped text/html", response("", "text/html", { status: 204 })],
    ["an empty 200 body", response("", undefined)],
    ["an undefined body", response(undefined, undefined)],
    ["a blob download", response(new Blob(["a,b"]), "text/csv", { responseType: "blob" })],
    ["a blob that is HTML", response(new Blob([SPA_PAGE]), "text/html", { responseType: "blob" })],
    ["a text download", response("a,b\n", "text/csv", { responseType: "text" })],
    ["an arraybuffer", response(new ArrayBuffer(2), "application/octet-stream", { responseType: "arraybuffer" })],
  ];
  it.each(passedCases)("passes %s", (_label, resp) => {
    expect(rejectUnreadableAnswer(resp)).toBe(resp);
  });

  it("isUnreadableAnswer is false for other errors", () => {
    expect(isUnreadableAnswer(new AxiosError("Network Error", "ERR_NETWORK"))).toBe(false);
    expect(isUnreadableAnswer(new Error("x"))).toBe(false);
    expect(isUnreadableAnswer(null)).toBe(false);
  });
});

describe("apiClient with the guard (issue #1320)", () => {
  it("rejects a 200 HTML page on a JSON request", async () => {
    server.use(http.get("/api/inventory/drivers", () => HttpResponse.html(SPA_PAGE)));
    const err = await apiClient.get("/inventory/drivers").catch((e: unknown) => e);
    expect(isUnreadableAnswer(err)).toBe(true);
  });

  it("still resolves a JSON answer and a 204", async () => {
    server.use(
      http.get("/api/inventory/drivers", () => HttpResponse.json({ items: [] })),
      http.delete("/api/inventory/drivers/d1", () => new HttpResponse(null, { status: 204 })),
    );
    await expect(apiClient.get("/inventory/drivers")).resolves.toMatchObject({
      data: { items: [] },
    });
    await expect(apiClient.delete("/inventory/drivers/d1")).resolves.toMatchObject({ status: 204 });
  });

  it("still downloads the report CSV, a text response", async () => {
    server.use(
      http.get("/api/reservations/reports/utilization.csv", () =>
        HttpResponse.text("user,hours\n", {
          headers: {
            "content-type": "text/csv",
            "content-disposition": 'attachment; filename="utilization-user.csv"',
          },
        }),
      ),
    );
    const out = await downloadUtilizationCsv({ start: "a", end: "b" }, "user");
    expect(out).toEqual({ filename: "utilization-user.csv", body: "user,hours\n" });
  });

  describe("exports, a blob response", () => {
    let clicked = 0;
    beforeEach(() => {
      clicked = 0;
      vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:x");
      vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
      vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {
        clicked += 1;
      });
    });
    afterEach(() => vi.restoreAllMocks());

    it("still downloads a device export through the real client's interceptors", async () => {
      // jsdom's XHR cannot hand MSW a blob body, so this one request goes
      // through a stub adapter; axios still runs apiClient's response
      // interceptors on what it returns.
      const original = apiClient.defaults.adapter;
      apiClient.defaults.adapter = async (config) => ({
        data: new Blob(["name\n"]),
        status: 200,
        statusText: "OK",
        headers: { "content-type": "text/csv" },
        config,
        request: {},
      });
      try {
        await exportDevices("csv");
      } finally {
        apiClient.defaults.adapter = original;
      }
      expect(clicked).toBe(1);
    });

    it("the same adapter's HTML answer to a JSON request is refused", async () => {
      const original = apiClient.defaults.adapter;
      apiClient.defaults.adapter = async (config) => ({
        data: SPA_PAGE,
        status: 200,
        statusText: "OK",
        headers: { "content-type": "text/html" },
        config,
        request: {},
      });
      try {
        const err = await apiClient.get("/inventory/devices").catch((e: unknown) => e);
        expect(isUnreadableAnswer(err)).toBe(true);
      } finally {
        apiClient.defaults.adapter = original;
      }
    });
  });

  it("the config client refuses a 200 HTML page too", async () => {
    server.use(http.get("/api/config/schema", () => HttpResponse.html(SPA_PAGE)));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useConfigSchema(), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));
    // The client's own refusal, not a later "data is undefined" from the hook.
    expect(isUnreadableAnswer(result.current.error)).toBe(true);
  });

  it("the About page still reads an HTML /version answer as an invalid response", async () => {
    server.use(http.get("/api/inventory/version", () => HttpResponse.html(SPA_PAGE)));
    await expect(
      fetchServiceVersion({ name: "inventory", label: "Inventory", path: "/inventory/version" }),
    ).rejects.toBeInstanceOf(InvalidVersionResponseError);
  });
});
