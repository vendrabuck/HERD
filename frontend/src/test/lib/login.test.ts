import {
  DEFAULT_AFTER_LOGIN,
  LOGIN_INVALID_CREDENTIALS,
  LOGIN_RATE_LIMITED,
  LOGIN_UNEXPECTED,
  LOGIN_UNREACHABLE,
  loginErrorText,
  returnPathFromState,
  safeReturnPath,
} from "@/lib/login";

// Issue #1333: the return path recorded by AuthGuard is honored only when it
// is a same-origin absolute path.
describe("safeReturnPath", () => {
  it.each([
    "/topology",
    "/topology/3f1c?tab=wiring#node-1",
    "/reservations?status=ACTIVE",
    "/admin/users",
    "/a%2F%2Fb",
  ])("accepts the same-origin path %s", (path) => {
    expect(safeReturnPath(path)).toBe(path);
  });

  it.each([
    ["a protocol-relative URL", "//evil.example"],
    ["a protocol-relative URL with a path", "//evil.example/topology"],
    ["a backslash after the slash", "/\\evil.example"],
    ["a backslash anywhere", "/topology\\..\\x"],
    ["an absolute https URL", "https://evil.example/"],
    ["a javascript: URL", "javascript:alert(1)"],
    ["a data: URL", "data:text/html,x"],
    ["a relative path", "topology"],
    ["a tab", "/\t/evil.example"],
    ["a newline", "/topology\n"],
    ["a NUL", "/topology\u0000"],
    ["DEL", "/topology\u007f"],
    ["a C1 control", "/topology\u0085"],
    ["the empty string", ""],
    ["the login page", "/login"],
    ["the login page with a query", "/login?x=1"],
    ["the register page", "/register"],
  ])("refuses %s", (_label, path) => {
    expect(safeReturnPath(path)).toBeNull();
  });

  it.each([null, undefined, 42, {}, ["/topology"], { pathname: "/topology" }])(
    "refuses the non-string %j",
    (value) => {
      expect(safeReturnPath(value)).toBeNull();
    },
  );
});

describe("returnPathFromState", () => {
  it("returns a safe recorded path", () => {
    expect(returnPathFromState({ from: "/inventory?q=x" })).toBe("/inventory?q=x");
  });

  it.each([null, undefined, "x", {}, { from: "//evil.example" }, { from: 7 }])(
    "falls back to the default page for %j",
    (state) => {
      expect(returnPathFromState(state)).toBe(DEFAULT_AFTER_LOGIN);
    },
  );
});

describe("loginErrorText", () => {
  const withStatus = (status: number, headers: Record<string, string> = {}) => ({
    response: { status, headers, data: { detail: "server text that must not show" } },
  });

  it("calls only a 401 a credential refusal", () => {
    expect(loginErrorText(withStatus(401))).toBe(LOGIN_INVALID_CREDENTIALS);
  });

  it.each([404, 500, 502, 503, 504])("calls a %i unreachable", (status) => {
    expect(loginErrorText(withStatus(status))).toBe(LOGIN_UNREACHABLE);
  });

  it.each([
    ["no response (network error)", { message: "Network Error", code: "ERR_NETWORK" }],
    ["a timeout", { message: "timeout of 1ms exceeded", code: "ECONNABORTED" }],
    ["a non-error value", "boom"],
    ["null", null],
  ])("calls %s unreachable", (_label, err) => {
    expect(loginErrorText(err)).toBe(LOGIN_UNREACHABLE);
  });

  it("gives a 429 the rate-limit wording, with the wait when Retry-After is whole seconds", () => {
    expect(loginErrorText(withStatus(429))).toBe(LOGIN_RATE_LIMITED);
    expect(loginErrorText(withStatus(429, { "Retry-After": "1" }))).toBe(
      "Too many sign-in attempts. Try again in 1 second.",
    );
    expect(loginErrorText(withStatus(429, { "Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT" }))).toBe(
      LOGIN_RATE_LIMITED,
    );
  });

  it.each([400, 403, 409])("gives an unmapped %i the unexpected-answer wording", (status) => {
    expect(loginErrorText(withStatus(status))).toBe(LOGIN_UNEXPECTED);
  });
});
