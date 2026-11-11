import { tokenSubject, useAuthStore } from "@/stores/authStore";
import { fakeJwt } from "../fixtures/jwt";

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
// dispatch the event by hand. Tokens are JWT-shaped (fakeJwt) because the
// store compares the accounts (`sub`) of the old and new token.
function storageEvent(key: string | null) {
  window.dispatchEvent(new StorageEvent("storage", { key, storageArea: localStorage }));
}

function storeAnother(access: string, refresh: string) {
  localStorage.setItem("refresh_token", refresh);
  localStorage.setItem("access_token", access);
}

const ALICE_1 = fakeJwt("alice", 1);
const ALICE_2 = fakeJwt("alice", 2);
const BOB_1 = fakeJwt("bob", 1);

describe("authStore cross-tab sync (issue #1316)", () => {
  beforeEach(() => {
    localStorage.clear();
    useAuthStore.setState({
      accessToken: null,
      refreshToken: null,
      user: null,
      isAuthenticated: false,
      lastSubject: null,
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("adopts tokens another tab stored when the storage event arrives", () => {
    useAuthStore.setState({ accessToken: ALICE_1, refreshToken: "R1", isAuthenticated: true });
    storeAnother(ALICE_2, "R2");
    // Before the event this tab still holds what it had.
    expect(useAuthStore.getState().accessToken).toBe(ALICE_1);
    storageEvent("access_token");
    const state = useAuthStore.getState();
    expect(state.accessToken).toBe(ALICE_2);
    expect(state.refreshToken).toBe("R2");
    expect(state.isAuthenticated).toBe(true);
    expect(state.lastSubject).toBe("alice");
  });

  it("signs this tab in when another tab logs in", () => {
    storeAnother(BOB_1, "R9");
    storageEvent("access_token");
    expect(useAuthStore.getState().isAuthenticated).toBe(true);
    expect(useAuthStore.getState().accessToken).toBe(BOB_1);
  });

  it("signs this tab out when another tab removes the tokens", () => {
    useAuthStore.getState().setTokens(ALICE_1, "R1");
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
    // The account that was signed in is remembered for the return path.
    expect(state.lastSubject).toBe("alice");
  });

  it("signs this tab out when another tab clears the whole storage (key null)", () => {
    useAuthStore.getState().setTokens(ALICE_1, "R1");
    localStorage.clear();
    storageEvent(null);
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });

  it("ignores a storage event for an unrelated key", () => {
    useAuthStore.setState({ accessToken: ALICE_1, refreshToken: "R1", isAuthenticated: true });
    localStorage.setItem("access_token", ALICE_2);
    storageEvent("herd-something-else");
    expect(useAuthStore.getState().accessToken).toBe(ALICE_1);
  });

  it("a same-account token adoption emits no sign-out edge", () => {
    useAuthStore.setState({ accessToken: ALICE_1, refreshToken: "R1", isAuthenticated: true });
    const edges: string[] = [];
    const stop = useAuthStore.subscribe((s, prev) => {
      if (prev.isAuthenticated !== s.isAuthenticated) edges.push(`${prev.isAuthenticated}>${s.isAuthenticated}`);
    });
    storeAnother(ALICE_2, "R2");
    storageEvent("access_token");
    stop();
    expect(edges).toEqual([]);
    expect(useAuthStore.getState().accessToken).toBe(ALICE_2);
  });

  it("a late-processed logout then login as another account passes through exactly one sign-out edge and ends as that account", async () => {
    useAuthStore.setState({
      accessToken: ALICE_1,
      refreshToken: "RA",
      isAuthenticated: true,
      lastSubject: "alice",
      user: {
        id: "alice",
        email: "alice@x",
        username: "alice",
        full_name: "Alice",
        contact_email: null,
        phone: null,
        is_active: true,
        role: "admin",
        created_at: "2024-01-01",
      },
    });
    const edges: string[] = [];
    const stop = useAuthStore.subscribe((s, prev) => {
      if (prev.isAuthenticated !== s.isAuthenticated) edges.push(`${prev.isAuthenticated}>${s.isAuthenticated}`);
    });
    // A frozen tab thaws: storage already holds bob's session, and both queued
    // events (alice's logout, bob's login) re-read that CURRENT storage.
    storeAnother(BOB_1, "RB");
    storageEvent("access_token");
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    expect(useAuthStore.getState().user).toBeNull();
    storageEvent("access_token");
    await vi.waitFor(() => expect(useAuthStore.getState().accessToken).toBe(BOB_1));
    await new Promise((resolve) => setTimeout(resolve, 5));
    stop();
    expect(edges).toEqual(["true>false", "false>true"]);
    const state = useAuthStore.getState();
    expect(state.isAuthenticated).toBe(true);
    expect(state.refreshToken).toBe("RB");
    expect(state.lastSubject).toBe("bob");
    expect(state.user).toBeNull();
  });

  it("a token whose account cannot be read counts as another account", async () => {
    useAuthStore.setState({ accessToken: ALICE_1, refreshToken: "R1", isAuthenticated: true });
    storeAnother("opaque-token", "R2");
    storageEvent("access_token");
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
    await vi.waitFor(() => expect(useAuthStore.getState().accessToken).toBe("opaque-token"));
  });

  it("keeps working from memory when storage throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    useAuthStore.setState({ accessToken: ALICE_1, refreshToken: "R1", isAuthenticated: true });
    // An unreadable storage tells this tab nothing, so the event is a no-op.
    storageEvent("access_token");
    expect(useAuthStore.getState().accessToken).toBe(ALICE_1);
    useAuthStore.getState().clearAuth();
    expect(useAuthStore.getState().isAuthenticated).toBe(false);
  });
});

// A failed write flips module state for the rest of the module's life, so
// each of these tests loads a fresh store.
describe("authStore with storage that fails a write (issue #1316 review)", () => {
  async function freshStore() {
    vi.resetModules();
    return (await import("@/stores/authStore")).useAuthStore;
  }

  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
    vi.resetModules();
  });

  it("writes the refresh token before the access token and removes the access token first", async () => {
    const store = await freshStore();
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
    store.getState().setTokens(ALICE_1, "R1");
    store.getState().clearAuth();
    expect(setOrder).toEqual(["refresh_token", "access_token"]);
    expect(removeOrder).toEqual(["access_token", "refresh_token"]);
  });

  it("after a write fails the tab stops reading storage, so it never adopts the older stored pair", async () => {
    storeAnother(ALICE_1, "R1");
    vi.resetModules();
    const fresh = await import("@/stores/authStore");
    expect(fresh.useAuthStore.getState().accessToken).toBe(ALICE_1);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    fresh.useAuthStore.getState().setTokens(ALICE_2, "R2");
    // Storage still holds the old pair; memory holds the new one.
    expect(localStorage.getItem("refresh_token")).toBe("R1");
    expect(fresh.readStoredTokens()).toBeNull();
    storageEvent("access_token");
    expect(fresh.useAuthStore.getState().accessToken).toBe(ALICE_2);
    expect(fresh.useAuthStore.getState().refreshToken).toBe("R2");
  });
});

describe("tokenSubject", () => {
  it("reads the sub claim of a JWT-shaped token", () => {
    expect(tokenSubject(fakeJwt("alice"))).toBe("alice");
  });

  it.each([null, undefined, "", "opaque", "a.b", "a.%%%.c", "a.bnVsbA.c"])(
    "is null for the unreadable token %j",
    (token) => {
      expect(tokenSubject(token)).toBeNull();
    },
  );
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
