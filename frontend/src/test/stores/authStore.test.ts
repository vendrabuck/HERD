import { useAuthStore } from "@/stores/authStore";

describe("authStore", () => {
  beforeEach(() => {
    localStorage.clear();
    useAuthStore.setState({
      accessToken: null,
      refreshToken: null,
      user: null,
      isAuthenticated: false,
    });
  });

  it("setTokens persists to localStorage and updates state", () => {
    useAuthStore.getState().setTokens("access-123", "refresh-456");
    const state = useAuthStore.getState();
    expect(state.accessToken).toBe("access-123");
    expect(state.refreshToken).toBe("refresh-456");
    expect(state.isAuthenticated).toBe(true);
    expect(localStorage.getItem("access_token")).toBe("access-123");
    expect(localStorage.getItem("refresh_token")).toBe("refresh-456");
  });

  it("clearAuth removes tokens and resets state", () => {
    useAuthStore.getState().setTokens("access-123", "refresh-456");
    useAuthStore.getState().clearAuth();
    const state = useAuthStore.getState();
    expect(state.accessToken).toBeNull();
    expect(state.refreshToken).toBeNull();
    expect(state.user).toBeNull();
    expect(state.isAuthenticated).toBe(false);
    expect(localStorage.getItem("access_token")).toBeNull();
    expect(localStorage.getItem("refresh_token")).toBeNull();
  });

  it("setUser sets user object", () => {
    const user = {
      id: "1",
      email: "test@test.com",
      username: "testuser",
      full_name: "testuser",
      contact_email: null,
      phone: null,
      is_active: true,
      role: "user",
      created_at: "2024-01-01",
    };
    useAuthStore.getState().setUser(user);
    expect(useAuthStore.getState().user).toEqual(user);
  });
});

// Issue #1142: the store's initializer runs once, when the module is first
// imported, so the tests above (which reset state with setState) never
// exercise it. Each test here seeds localStorage, drops the module cache, and
// imports a FRESH store so the real initializer reads what was seeded.
describe("authStore initializer", () => {
  async function freshStore() {
    vi.resetModules();
    const mod = await import("@/stores/authStore");
    return mod.useAuthStore;
  }

  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    localStorage.clear();
    vi.resetModules();
  });

  it("reads tokens from localStorage on initialization", async () => {
    localStorage.setItem("access_token", "stored-access");
    localStorage.setItem("refresh_token", "stored-refresh");
    const state = (await freshStore()).getState();
    expect(state.accessToken).toBe("stored-access");
    expect(state.refreshToken).toBe("stored-refresh");
    expect(state.user).toBeNull();
    expect(state.isAuthenticated).toBe(true);
  });

  it("initializes with null tokens when localStorage is empty", async () => {
    const state = (await freshStore()).getState();
    expect(state.accessToken).toBeNull();
    expect(state.refreshToken).toBeNull();
    expect(state.user).toBeNull();
    expect(state.isAuthenticated).toBe(false);
  });

  it("is not authenticated from a stored refresh token alone", async () => {
    localStorage.setItem("refresh_token", "stored-refresh");
    const state = (await freshStore()).getState();
    expect(state.accessToken).toBeNull();
    expect(state.refreshToken).toBe("stored-refresh");
    expect(state.isAuthenticated).toBe(false);
  });
});

// Issue #1316: every tab of a browser profile shares the two localStorage
// keys, and the browser fires a `storage` event in every OTHER tab when one
// tab writes them. jsdom does not fire that event for writes made in its own
// window, so these tests write storage the way another tab would and then
// dispatch the event by hand.
describe("authStore cross-tab sync (issue #1316)", () => {
  function storageEvent(key: string | null) {
    window.dispatchEvent(new StorageEvent("storage", { key, storageArea: localStorage }));
  }

  beforeEach(() => {
    localStorage.clear();
    useAuthStore.setState({
      accessToken: null,
      refreshToken: null,
      user: null,
      isAuthenticated: false,
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("adopts tokens another tab stored when the storage event arrives", () => {
    useAuthStore.setState({ accessToken: "T1", refreshToken: "R1", isAuthenticated: true });
    localStorage.setItem("refresh_token", "R2");
    localStorage.setItem("access_token", "T2");
    // Before the event this tab still holds what it had.
    expect(useAuthStore.getState().accessToken).toBe("T1");
    storageEvent("access_token");
    const state = useAuthStore.getState();
    expect(state.accessToken).toBe("T2");
    expect(state.refreshToken).toBe("R2");
    expect(state.isAuthenticated).toBe(true);
  });

  it("signs this tab in when another tab logs in", () => {
    localStorage.setItem("refresh_token", "R9");
    localStorage.setItem("access_token", "T9");
    storageEvent("access_token");
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
    expect(useAuthStore.getState().accessToken).toBe("T9");
  });

  it("signs this tab out when another tab removes the tokens", () => {
    useAuthStore.getState().setTokens("T1", "R1");
    useAuthStore.setState({
      user: {
        id: "1",
        email: "a@b.c",
        username: "a",
        full_name: "a",
        contact_email: null,
        phone: null,
        is_active: true,
        role: "user",
        created_at: "2024-01-01",
      },
    });
    localStorage.removeItem("access_token");
    localStorage.removeItem("refresh_token");
    storageEvent("access_token");
    const state = useAuthStore.getState();
    expect(state.accessToken).toBeNull();
    expect(state.refreshToken).toBeNull();
    expect(state.user).toBeNull();
    expect(state.isAuthenticated).toBe(false);
  });

  it("signs this tab out when another tab clears the whole storage (key null)", () => {
    useAuthStore.getState().setTokens("T1", "R1");
    localStorage.clear();
    storageEvent(null);
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });

  it("ignores a storage event for an unrelated key", () => {
    useAuthStore.setState({ accessToken: "T1", refreshToken: "R1", isAuthenticated: true });
    localStorage.setItem("access_token", "T2");
    storageEvent("herd-something-else");
    expect(useAuthStore.getState().accessToken).toBe("T1");
  });

  it("writes the refresh token before the access token and removes the access token first", () => {
    const setOrder: string[] = [];
    const removeOrder: string[] = [];
    const setItem = Storage.prototype.setItem;
    const removeItem = Storage.prototype.removeItem;
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, k, v) {
      setOrder.push(k);
      setItem.call(this, k, v);
    });
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(function (this: Storage, k) {
      removeOrder.push(k);
      removeItem.call(this, k);
    });
    useAuthStore.getState().setTokens("T1", "R1");
    useAuthStore.getState().clearAuth();
    expect(setOrder).toEqual(["refresh_token", "access_token"]);
    expect(removeOrder).toEqual(["access_token", "refresh_token"]);
  });

  it("keeps working from memory when storage throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    useAuthStore.getState().setTokens("T1", "R1");
    expect(useAuthStore.getState().accessToken).toBe("T1");
    // An unreadable storage tells this tab nothing, so the event is a no-op.
    storageEvent("access_token");
    expect(useAuthStore.getState().accessToken).toBe("T1");
    useAuthStore.getState().clearAuth();
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });
});

describe("authStore initializer with unusable storage (issue #1316)", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.resetModules();
  });

  it("starts signed out instead of throwing when getItem throws", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    vi.resetModules();
    const { useAuthStore: fresh } = await import("@/stores/authStore");
    const state = fresh.getState();
    expect(state.accessToken).toBeNull();
    expect(state.isAuthenticated).toBe(false);
  });
});
