import { describe, it, expect, beforeEach } from "vitest";
import { useConfigStore } from "@/stores/configStore";

describe("configStore", () => {
  beforeEach(() => {
    sessionStorage.clear();
    useConfigStore.setState({ configToken: null });
  });

  it("starts with no config token", () => {
    expect(useConfigStore.getState().configToken).toBeNull();
  });

  it("setConfigToken updates the state and persists to sessionStorage", () => {
    useConfigStore.getState().setConfigToken("abc");
    expect(useConfigStore.getState().configToken).toBe("abc");
    expect(sessionStorage.getItem("config_token")).toBe("abc");
  });

  it("clearConfigToken nulls the state and removes the sessionStorage entry", () => {
    useConfigStore.getState().setConfigToken("abc");
    useConfigStore.getState().clearConfigToken();
    expect(useConfigStore.getState().configToken).toBeNull();
    expect(sessionStorage.getItem("config_token")).toBeNull();
  });
});
