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
