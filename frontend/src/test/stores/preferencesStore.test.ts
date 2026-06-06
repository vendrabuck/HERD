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

  it("getSavedFilter falls back when missing", () => {
    const fallback = { search: "" };
    const got = usePreferencesStore.getState().getSavedFilter("inventory", fallback);
    expect(got).toBe(fallback);
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
});
