import { vi, type Mock } from "vitest";
import { usePreferencesStore, _flushPendingPatchForTest } from "@/stores/preferencesStore";
import * as userProfileApi from "@/api/userProfile";

vi.mock("@/api/userProfile", () => ({
  getPreferences: vi.fn(),
  patchPreferences: vi.fn(),
  resetPreferences: vi.fn(),
}));

const mockedGet = userProfileApi.getPreferences as unknown as Mock;
const mockedPatch = userProfileApi.patchPreferences as unknown as Mock;

describe("preferencesStore", () => {
  beforeEach(() => {
    mockedGet.mockReset();
    mockedPatch.mockReset();
    mockedPatch.mockResolvedValue({
      user_id: "u",
      saved_filters: {},
      page_sizes: {},
      extras: {},
      updated_at: "",
    });
    usePreferencesStore.getState().clear();
    // Most cases model a loaded store; the issue #985 block below starts from
    // the pre-load state on purpose.
    usePreferencesStore.setState({ loaded: true });
  });

  it("load hydrates state from the backend", async () => {
    mockedGet.mockResolvedValue({
      user_id: "u",
      saved_filters: { inventory: { search: "router" } },
      page_sizes: { inventory: 25 },
      extras: { theme: "dark" },
      updated_at: "2026-04-20",
    });
    await usePreferencesStore.getState().load();
    const s = usePreferencesStore.getState();
    expect(s.loaded).toBe(true);
    expect(s.savedFilters).toEqual({ inventory: { search: "router" } });
    expect(s.pageSizes).toEqual({ inventory: 25 });
    expect(s.extras).toEqual({ theme: "dark" });
  });

  it("load marks loaded even on failure", async () => {
    mockedGet.mockRejectedValue(new Error("nope"));
    await usePreferencesStore.getState().load();
    expect(usePreferencesStore.getState().loaded).toBe(true);
  });

  it("setSavedFilter updates state and queues a debounced patch", () => {
    usePreferencesStore.getState().setSavedFilter("inventory", { search: "r" });
    expect(usePreferencesStore.getState().savedFilters).toEqual({
      inventory: { search: "r" },
    });
    expect(mockedPatch).not.toHaveBeenCalled();
    _flushPendingPatchForTest();
    expect(mockedPatch).toHaveBeenCalledTimes(1);
    expect(mockedPatch.mock.calls[0][0].saved_filters).toEqual({
      inventory: { search: "r" },
    });
  });

  it("setPageSize updates state and queues a debounced patch", () => {
    usePreferencesStore.getState().setPageSize("inventory", 25);
    expect(usePreferencesStore.getState().pageSizes).toEqual({ inventory: 25 });
    _flushPendingPatchForTest();
    expect(mockedPatch).toHaveBeenCalledTimes(1);
    expect(mockedPatch.mock.calls[0][0].page_sizes).toEqual({ inventory: 25 });
  });

  it("rapid updates coalesce into one patch", () => {
    const s = usePreferencesStore.getState();
    s.setSavedFilter("inventory", { search: "a" });
    s.setSavedFilter("inventory", { search: "ab" });
    s.setPageSize("inventory", 25);
    _flushPendingPatchForTest();
    expect(mockedPatch).toHaveBeenCalledTimes(1);
    const payload = mockedPatch.mock.calls[0][0];
    expect(payload.saved_filters).toEqual({ inventory: { search: "ab" } });
    expect(payload.page_sizes).toEqual({ inventory: 25 });
  });

  it("clear resets state and drops pending patches", () => {
    usePreferencesStore.getState().setPageSize("inventory", 25);
    usePreferencesStore.getState().clear();
    _flushPendingPatchForTest();
    expect(mockedPatch).not.toHaveBeenCalled();
    expect(usePreferencesStore.getState().pageSizes).toEqual({});
    expect(usePreferencesStore.getState().loaded).toBe(false);
  });

  // Sort state (issue #844): stored under the generic `extras` bucket, keyed
  // per page, the same shape savedFilters/pageSizes use.
  it("getSortState returns null when nothing is stored for the page", () => {
    expect(usePreferencesStore.getState().getSortState("reservations")).toBeNull();
  });

  it("setSortState updates state, round-trips through getSortState, and queues a debounced patch", () => {
    usePreferencesStore.getState().setSortState("reservations", {
      sortBy: "start_time",
      sortDir: "asc",
    });
    expect(usePreferencesStore.getState().getSortState("reservations")).toEqual({
      sortBy: "start_time",
      sortDir: "asc",
    });
    expect(mockedPatch).not.toHaveBeenCalled();
    _flushPendingPatchForTest();
    expect(mockedPatch).toHaveBeenCalledTimes(1);
    expect(mockedPatch.mock.calls[0][0].extras).toEqual({
      "sort:reservations": { sortBy: "start_time", sortDir: "asc" },
    });
  });

  it("setSortState(page, null) clears the stored sort but still patches the key", () => {
    usePreferencesStore.getState().setSortState("reservations", {
      sortBy: "status",
      sortDir: "desc",
    });
    usePreferencesStore.getState().setSortState("reservations", null);
    expect(usePreferencesStore.getState().getSortState("reservations")).toBeNull();
    _flushPendingPatchForTest();
    // The last queued write for this key wins (coalesced, same as the
    // savedFilters/pageSizes rapid-update behavior above).
    expect(mockedPatch.mock.calls[0][0].extras).toEqual({
      "sort:reservations": null,
    });
  });

  it("sort state is keyed per page, independent of other pages' extras", () => {
    usePreferencesStore.getState().setSortState("reservations", {
      sortBy: "status",
      sortDir: "asc",
    });
    expect(usePreferencesStore.getState().getSortState("other-page")).toBeNull();
  });

  it("getSortState ignores a malformed stored value (a foreign or stale extras entry)", () => {
    usePreferencesStore.setState({ extras: { "sort:reservations": { sortBy: "start_time" } } });
    expect(usePreferencesStore.getState().getSortState("reservations")).toBeNull();
    usePreferencesStore.setState({ extras: { "sort:reservations": "not-an-object" } });
    expect(usePreferencesStore.getState().getSortState("reservations")).toBeNull();
  });
});

// Issue #985: a write made while the preferences GET is in flight was built
// from the unloaded defaults (an empty search). It must never reach the server
// as built; only what the user changed is applied on top of the loaded value.
describe("preferencesStore writes made before the load resolves (issue #985)", () => {
  const prefs = (overrides: Record<string, unknown> = {}) => ({
    user_id: "u",
    saved_filters: {},
    page_sizes: {},
    extras: {},
    updated_at: "",
    ...overrides,
  });

  function deferredGet() {
    let resolve!: (value: unknown) => void;
    let reject!: (reason: unknown) => void;
    mockedGet.mockReturnValue(
      new Promise((res, rej) => {
        resolve = res;
        reject = rej;
      }),
    );
    return { resolve, reject };
  }

  beforeEach(() => {
    mockedGet.mockReset();
    mockedPatch.mockReset();
    mockedPatch.mockResolvedValue(prefs());
    usePreferencesStore.getState().clear();
  });

  it("a status change before the load keeps the saved search (the issue's observation)", async () => {
    const pending = deferredGet();
    const loading = usePreferencesStore.getState().load();
    // The page serializes its whole filter from the unloaded defaults.
    usePreferencesStore.getState().setSavedFilter("inventory", {
      search: "",
      status: "OFFLINE",
    });
    _flushPendingPatchForTest();
    expect(mockedPatch).not.toHaveBeenCalled();

    pending.resolve(prefs({ saved_filters: { inventory: { search: "foo" } } }));
    await loading;

    expect(usePreferencesStore.getState().savedFilters.inventory).toEqual({
      search: "foo",
      status: "OFFLINE",
    });
    _flushPendingPatchForTest();
    expect(mockedPatch).toHaveBeenCalledTimes(1);
    expect(mockedPatch.mock.calls[0][0].saved_filters).toEqual({
      inventory: { search: "foo", status: "OFFLINE" },
    });
  });

  it("nothing is sent while the load is pending, however many writes are made", () => {
    deferredGet();
    void usePreferencesStore.getState().load();
    const s = usePreferencesStore.getState();
    s.setSavedFilter("inventory", { search: "", status: "OFFLINE" });
    s.setPageSize("inventory", 100);
    s.setSortState("reservations", { sortBy: "status", sortDir: "asc" });
    _flushPendingPatchForTest();
    expect(mockedPatch).not.toHaveBeenCalled();
    // The screen reflects the user's choices at once.
    expect(usePreferencesStore.getState().pageSizes.inventory).toBe(100);
  });

  it("a key the user set and then returned to its default overrides the loaded value", async () => {
    const pending = deferredGet();
    const loading = usePreferencesStore.getState().load();
    const s = usePreferencesStore.getState();
    s.setSavedFilter("inventory", { search: "", status: "OFFLINE" });
    s.setSavedFilter("inventory", { search: "" });
    pending.resolve(
      prefs({ saved_filters: { inventory: { search: "foo", status: "RESERVED", topology_type: "CLOUD" } } }),
    );
    await loading;
    // status was chosen (All); search and topology were never touched.
    expect(usePreferencesStore.getState().savedFilters.inventory).toEqual({
      search: "foo",
      topology_type: "CLOUD",
    });
  });

  it("a search typed before the load wins over the saved search", async () => {
    const pending = deferredGet();
    const loading = usePreferencesStore.getState().load();
    usePreferencesStore.getState().setSavedFilter("topologies", { search: "edge" });
    pending.resolve(prefs({ saved_filters: { topologies: { search: "foo", owner: "mine" } } }));
    await loading;
    _flushPendingPatchForTest();
    expect(mockedPatch.mock.calls[0][0].saved_filters).toEqual({
      topologies: { search: "edge", owner: "mine" },
    });
  });

  it("a write that changes nothing the user chose still sends no defaults over the saved values", async () => {
    const pending = deferredGet();
    const loading = usePreferencesStore.getState().load();
    usePreferencesStore.getState().setSavedFilter("reservations", { search: "" });
    pending.resolve(prefs({ saved_filters: { reservations: { search: "lab", period: "past" } } }));
    await loading;
    expect(usePreferencesStore.getState().savedFilters.reservations).toEqual({
      search: "lab",
      period: "past",
    });
    _flushPendingPatchForTest();
    expect(mockedPatch.mock.calls[0][0].saved_filters).toEqual({
      reservations: { search: "lab", period: "past" },
    });
  });

  it("page sizes and sort chosen before the load win over the loaded values and other pages are kept", async () => {
    const pending = deferredGet();
    const loading = usePreferencesStore.getState().load();
    const s = usePreferencesStore.getState();
    s.setPageSize("inventory", 100);
    s.setSortState("reservations", { sortBy: "status", sortDir: "desc" });
    pending.resolve(
      prefs({
        saved_filters: { inventory: { search: "foo" } },
        page_sizes: { inventory: 25, topologies: 10 },
        extras: { "sort:reservations": { sortBy: "start_time", sortDir: "asc" }, theme: "dark" },
      }),
    );
    await loading;
    const after = usePreferencesStore.getState();
    expect(after.pageSizes).toEqual({ inventory: 100, topologies: 10 });
    expect(after.getSortState("reservations")).toEqual({ sortBy: "status", sortDir: "desc" });
    expect(after.extras.theme).toBe("dark");
    expect(after.savedFilters).toEqual({ inventory: { search: "foo" } });
    _flushPendingPatchForTest();
    expect(mockedPatch).toHaveBeenCalledTimes(1);
    const payload = mockedPatch.mock.calls[0][0];
    expect(payload.saved_filters).toEqual({});
    expect(payload.page_sizes).toEqual({ inventory: 100 });
    expect(payload.extras).toEqual({ "sort:reservations": { sortBy: "status", sortDir: "desc" } });
  });

  it("a failed load still lets the held writes through, merged over the defaults", async () => {
    const pending = deferredGet();
    const loading = usePreferencesStore.getState().load();
    usePreferencesStore.getState().setSavedFilter("inventory", { search: "", status: "OFFLINE" });
    pending.reject(new Error("down"));
    await loading;
    expect(usePreferencesStore.getState().loaded).toBe(true);
    _flushPendingPatchForTest();
    expect(mockedPatch).toHaveBeenCalledTimes(1);
    expect(mockedPatch.mock.calls[0][0].saved_filters).toEqual({
      inventory: { status: "OFFLINE" },
    });
  });

  it("a load with no writes made while it was pending sends nothing", async () => {
    mockedGet.mockResolvedValue(prefs({ saved_filters: { inventory: { search: "foo" } } }));
    await usePreferencesStore.getState().load();
    _flushPendingPatchForTest();
    expect(mockedPatch).not.toHaveBeenCalled();
  });

  it("writes after the load go out as before", async () => {
    mockedGet.mockResolvedValue(prefs());
    await usePreferencesStore.getState().load();
    usePreferencesStore.getState().setSavedFilter("inventory", { search: "r" });
    _flushPendingPatchForTest();
    expect(mockedPatch.mock.calls[0][0].saved_filters).toEqual({ inventory: { search: "r" } });
  });

  it("clear drops writes held for a load", async () => {
    const pending = deferredGet();
    const loading = usePreferencesStore.getState().load();
    usePreferencesStore.getState().setSavedFilter("inventory", { search: "", status: "OFFLINE" });
    usePreferencesStore.getState().clear();
    pending.resolve(prefs());
    await loading;
    _flushPendingPatchForTest();
    expect(mockedPatch).not.toHaveBeenCalled();
  });
});
