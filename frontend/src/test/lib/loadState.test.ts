import { describe, it, expect } from "vitest";
import { loadState, noEntries, noItems, type LoadState } from "@/lib/loadState";
import { LOAD_FAILURE_NO_ANSWER, loadFailureDetail } from "@/lib/errors";

describe("loadState (issue #1320)", () => {
  // The whole input space: data absent, empty, or present; the last fetch
  // failed or not. A failure without data is "error", never "empty".
  const cases: Array<[string, unknown[] | undefined, boolean, LoadState]> = [
    ["no data, no failure", undefined, false, "loading"],
    ["no data, failed", undefined, true, "error"],
    ["empty answer", [], false, "empty"],
    ["empty answer, later refetch failed", [], true, "empty"],
    ["rows", [1], false, "loaded"],
    ["rows, later refetch failed", [1], true, "loaded"],
  ];
  it.each(cases)("%s is %s", (_label, data, isError, expected) => {
    expect(loadState({ data, isError }, noEntries)).toBe(expected);
  });

  it("without an isEmpty predicate, any data is loaded", () => {
    expect(loadState({ data: [], isError: false })).toBe("loaded");
    expect(loadState({ data: undefined, isError: true })).toBe("error");
  });

  it("noItems reads a paginated answer's items", () => {
    expect(loadState({ data: { items: [] }, isError: false }, noItems)).toBe("empty");
    expect(loadState({ data: { items: [1] }, isError: false }, noItems)).toBe("loaded");
  });

  it("a failure with no data is error even while a refetch is in flight", () => {
    // TanStack Query keeps status "error" (and isError true) during a refetch.
    expect(loadState({ data: undefined, isError: true }, noEntries)).toBe("error");
  });
});

describe("loadFailureDetail (issue #1320)", () => {
  it("passes the server's string detail through", () => {
    expect(loadFailureDetail({ response: { status: 500, data: { detail: "db down" } } })).toBe(
      "db down",
    );
  });

  it("formats a 422 list through detailText", () => {
    expect(
      loadFailureDetail({
        response: {
          status: 422,
          data: { detail: [{ loc: ["query", "limit"], msg: "too big", type: "x" }] },
        },
      }),
    ).toBe("limit: too big");
  });

  it("names the HTTP status when the body has no usable detail", () => {
    expect(loadFailureDetail({ response: { status: 503, data: "<html>bad gateway</html>" } })).toBe(
      "The server answered with HTTP 503.",
    );
    expect(loadFailureDetail({ response: { status: 500, data: { detail: { error: "x" } } } })).toBe(
      "The server answered with HTTP 500.",
    );
  });

  it("says the server did not answer when no response arrived", () => {
    expect(loadFailureDetail(new Error("Network Error"))).toBe(LOAD_FAILURE_NO_ANSWER);
    expect(loadFailureDetail(null)).toBe(LOAD_FAILURE_NO_ANSWER);
    expect(loadFailureDetail(undefined)).toBe(LOAD_FAILURE_NO_ANSWER);
    expect(LOAD_FAILURE_NO_ANSWER).toBe("The server did not answer.");
  });

  it("never shows the transport message or a non-integer status", () => {
    const err = Object.assign(new Error("Request failed with status code 502"), {
      response: { status: "502<script>", data: {} },
    });
    expect(loadFailureDetail(err)).toBe(LOAD_FAILURE_NO_ANSWER);
    expect(loadFailureDetail({ response: { status: 500.5, data: {} } })).toBe(
      LOAD_FAILURE_NO_ANSWER,
    );
  });
});
