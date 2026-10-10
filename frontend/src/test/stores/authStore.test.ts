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
