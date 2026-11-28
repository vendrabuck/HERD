import { describe, it, expect } from "vitest";
import { AxiosError } from "axios";
import {
  isPlainObject,
  loadState,
  noEntries,
  noItems,
  refreshFailed,
  type LoadState,
} from "@/lib/loadState";
import {
  LOAD_FAILURE_NO_ANSWER,
  LOAD_FAILURE_UNREADABLE,
  loadFailureDetail,
} from "@/lib/errors";

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
});

describe("loadState never throws, and reads an unusable shape as error (issue #1320 review)", () => {
  it("an isEmpty that throws is error, not a crash", () => {
    const boom = (): boolean => {
      throw new TypeError("boom");
    };
    expect(loadState({ data: "<html>", isError: false }, boom)).toBe("error");
  });

  it("noItems and noEntries throw on the wrong shape, which loadState reads as error", () => {
    expect(loadState({ data: "<html>" as never, isError: false }, noItems)).toBe("error");
    expect(loadState({ data: { unexpected: true } as never, isError: false }, noItems)).toBe(
      "error",
    );
    expect(loadState({ data: { items: "x" } as never, isError: false }, noItems)).toBe("error");
    expect(loadState({ data: {} as never, isError: false }, noEntries)).toBe("error");
    expect(loadState({ data: "abc" as never, isError: false }, noEntries)).toBe("error");
  });

  it("isValid false, or throwing, is error", () => {
    expect(loadState({ data: [], isError: false }, undefined, isPlainObject)).toBe("error");
    expect(loadState({ data: "x", isError: false }, undefined, isPlainObject)).toBe("error");
    expect(loadState({ data: null, isError: false }, undefined, isPlainObject)).toBe("error");
    expect(loadState({ data: { a: 1 }, isError: false }, undefined, isPlainObject)).toBe(
      "loaded",
    );
    const boom = (): boolean => {
      throw new Error("x");
    };
    expect(loadState({ data: 1, isError: false }, undefined, boom)).toBe("error");
  });
});

describe("refreshFailed (issue #1320 review)", () => {
  const cases: Array<[string, unknown[] | undefined, boolean, boolean]> = [
    ["no data, failed (the full error instead)", undefined, true, false],
    ["no data, no failure", undefined, false, false],
    ["rows, latest fetch failed", [1], true, true],
    ["empty answer, latest fetch failed", [], true, true],
    ["rows, no failure", [1], false, false],
  ];
  it.each(cases)("%s is %s", (_label, data, isError, expected) => {
    expect(refreshFailed({ data, isError }, noEntries)).toBe(expected);
  });

  it("is false when the kept data is of an unusable shape (that is the full error)", () => {
    expect(refreshFailed({ data: "<html>" as never, isError: true }, noItems)).toBe(false);
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

  it("says the server did not answer only when an axios request got no response", () => {
    expect(loadFailureDetail(new AxiosError("Network Error", "ERR_NETWORK"))).toBe(
      LOAD_FAILURE_NO_ANSWER,
    );
    expect(loadFailureDetail(new AxiosError("timeout", "ECONNABORTED"))).toBe(
      LOAD_FAILURE_NO_ANSWER,
    );
    expect(LOAD_FAILURE_NO_ANSWER).toBe("The server did not answer.");
  });

  it("says the answer could not be read for any other failure, the server having answered", () => {
    // A body of the wrong shape (TanStack Query's own error, a TypeError), the
    // client's refusal of an HTML 2xx, or no error object at all.
    expect(loadFailureDetail(new Error("Query data cannot be undefined"))).toBe(
      LOAD_FAILURE_UNREADABLE,
    );
    expect(loadFailureDetail(new TypeError("x.items is undefined"))).toBe(LOAD_FAILURE_UNREADABLE);
    expect(loadFailureDetail(new AxiosError("not JSON", "ERR_UNREADABLE_ANSWER"))).toBe(
      LOAD_FAILURE_UNREADABLE,
    );
    expect(loadFailureDetail(null)).toBe(LOAD_FAILURE_UNREADABLE);
    expect(loadFailureDetail(undefined)).toBe(LOAD_FAILURE_UNREADABLE);
    expect(LOAD_FAILURE_UNREADABLE).toBe("The server's answer could not be read.");
  });

  it("never shows the transport message or a non-integer status", () => {
    const err = Object.assign(new Error("Request failed with status code 502"), {
      response: { status: "502<script>", data: {} },
    });
    expect(loadFailureDetail(err)).toBe(LOAD_FAILURE_UNREADABLE);
    expect(loadFailureDetail({ response: { status: 500.5, data: {} } })).toBe(
      LOAD_FAILURE_UNREADABLE,
    );
  });
});
