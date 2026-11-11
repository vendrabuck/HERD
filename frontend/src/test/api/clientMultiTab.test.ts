import { http, HttpResponse } from "msw";
import type { AxiosInstance } from "axios";
import { server } from "../mocks/server";

/*
 * Issue #1316: two tabs of one browser profile, simulated as two fresh module
 * instances of the auth store and the API client (vi.resetModules between the
 * two imports) over jsdom's one localStorage, which both share exactly as two
 * real tabs share theirs. The `storage` event that a real browser fires in
 * the OTHER tab is dispatched by hand (jsdom fires none for its own window's
 * writes); both instances listen on the same window, so the writing tab
 * receives it too, which is harmless because a sync to what storage already
 * holds is a no-op.
 *
 * What this simulation cannot prove about real browsers: that the browser
 * delivers the storage event to the other tab, and how late (cross-process
 * propagation, a throttled background tab); that real Web Locks serialize
 * across processes the way the fake below does; and real network timing. The
 * Playwright test tests/e2e/test_multi_tab_session_playwright.py is the proof
 * for those, written for the nightly and gate runs.
 *
 * The auth model below matches services/auth: a refresh consumes the
 * presented token atomically when the request ARRIVES (IAM-SESSION-2), mints
 * T(n)/R(n), and refuses a used token with 401; the response can be delayed
 * separately, which is how the interleavings are staged.
 */

interface Tab {
  client: AxiosInstance;
  store: typeof import("@/stores/authStore").useAuthStore;
  stop: () => void;
}

const openTabs: Tab[] = [];

async function openTab(): Promise<Tab> {
  vi.resetModules();
  const storeModule = await import("@/stores/authStore");
  const clientModule = await import("@/api/client");
  const tab = {
    client: clientModule.default,
    store: storeModule.useAuthStore,
    stop: storeModule.stopStorageSync,
  };
  openTabs.push(tab);
  return tab;
}

function deliverStorageEvent(key: string | null = "access_token") {
  window.dispatchEvent(new StorageEvent("storage", { key, storageArea: localStorage }));
}

function signIn(access: string, refresh: string) {
  localStorage.setItem("refresh_token", refresh);
  localStorage.setItem("access_token", access);
}

function stored() {
  return {
    access: localStorage.getItem("access_token"),
    refresh: localStorage.getItem("refresh_token"),
  };
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

interface AuthModel {
  refreshCalls: string[];
  logoutCalls: string[];
  /** Delay (ms) before the n-th refresh response (0-based); default 0. */
  refreshDelays: number[];
  /** Delay (ms) before each protected response; default 0. */
  protectedDelay: number;
  live: Set<string>;
  validAccess: Set<string>;
}

function installAuthModel(liveRefresh: string[] = ["R1"]): AuthModel {
  const model: AuthModel = {
    refreshCalls: [],
    logoutCalls: [],
    refreshDelays: [],
    protectedDelay: 0,
    live: new Set(liveRefresh),
    validAccess: new Set(),
  };
  let minted = 1;
  server.use(
    http.post("/api/auth/refresh", async ({ request }) => {
      const body = (await request.json()) as { refresh_token: string };
      const index = model.refreshCalls.length;
      model.refreshCalls.push(body.refresh_token);
      const accepted = model.live.delete(body.refresh_token);
      let pair: { access_token: string; refresh_token: string } | null = null;
      if (accepted) {
        minted += 1;
        pair = { access_token: `T${minted}`, refresh_token: `R${minted}` };
        model.live.add(pair.refresh_token);
        model.validAccess.add(pair.access_token);
      }
      await sleep(model.refreshDelays[index] ?? 0);
      if (!pair) {
        return HttpResponse.json(
          { detail: "Invalid or expired refresh token" },
          { status: 401 },
        );
      }
      return HttpResponse.json(pair);
    }),
    http.post("/api/auth/logout", async ({ request }) => {
      const body = (await request.json()) as { refresh_token: string };
      model.logoutCalls.push(body.refresh_token);
      model.live.delete(body.refresh_token);
      return new HttpResponse(null, { status: 204 });
    }),
    http.get("/api/thing", async ({ request }) => {
      const token = (request.headers.get("Authorization") ?? "").replace("Bearer ", "");
      await sleep(model.protectedDelay);
      if (!model.validAccess.has(token)) {
        return HttpResponse.json({ detail: "Could not validate credentials" }, { status: 401 });
      }
      return HttpResponse.json({ token });
    }),
  );
  return model;
}

/** A fake LockManager shared by every tab: one FIFO exclusive lock per name. */
function installFakeWebLocks() {
  const tails = new Map<string, Promise<unknown>>();
  const fake = {
    grants: 0,
    request<T>(name: string, callback: () => Promise<T>): Promise<T> {
      const previous = tails.get(name) ?? Promise.resolve();
      const run = previous.then(() => {
        fake.grants += 1;
        return callback();
      });
      tails.set(
        name,
        run.catch(() => undefined),
      );
      return run;
    },
  };
  Object.defineProperty(navigator, "locks", { value: fake, configurable: true });
  return fake;
}

function removeWebLocks() {
  if (Object.prototype.hasOwnProperty.call(navigator, "locks")) {
    delete (navigator as unknown as { locks?: unknown }).locks;
  }
}

beforeEach(() => {
  localStorage.clear();
  removeWebLocks();
});

afterEach(() => {
  for (const tab of openTabs.splice(0)) tab.stop();
  removeWebLocks();
  vi.restoreAllMocks();
  localStorage.clear();
  vi.resetModules();
});

describe("two tabs share one rotating session (issue #1316)", () => {
  it("A refreshes, then B's request with the rotated-away token 401s: B retries with A's token and never posts R1", async () => {
    const model = installAuthModel();
    signIn("T1", "R1");
    const a = await openTab();
    const b = await openTab();

    await expect(a.client.get("/thing")).resolves.toMatchObject({ data: { token: "T2" } });
    // No storage event reaches B: its memory still holds T1 and R1.
    expect(b.store.getState().accessToken).toBe("T1");

    await expect(b.client.get("/thing")).resolves.toMatchObject({ data: { token: "T2" } });
    expect(model.refreshCalls).toEqual(["R1"]);
    expect(b.store.getState().accessToken).toBe("T2");
    expect(b.store.getState().refreshToken).toBe("R2");
    expect(stored()).toEqual({ access: "T2", refresh: "R2" });
  });

  it("A refreshes and the storage event reaches B: B's next request carries the new token at once", async () => {
    const model = installAuthModel();
    signIn("T1", "R1");
    const a = await openTab();
    const b = await openTab();

    await a.client.get("/thing");
    deliverStorageEvent();
    expect(b.store.getState().accessToken).toBe("T2");

    await expect(b.client.get("/thing")).resolves.toMatchObject({ data: { token: "T2" } });
    expect(model.refreshCalls).toEqual(["R1"]);
  });

  it("both tabs 401 at the same moment with Web Locks: one refresh serves both", async () => {
    const model = installAuthModel();
    model.refreshDelays = [30];
    const locks = installFakeWebLocks();
    signIn("T1", "R1");
    const a = await openTab();
    const b = await openTab();

    const [ra, rb] = await Promise.all([a.client.get("/thing"), b.client.get("/thing")]);
    expect(ra.data).toEqual({ token: "T2" });
    expect(rb.data).toEqual({ token: "T2" });
    expect(model.refreshCalls).toEqual(["R1"]);
    expect(locks.grants).toBe(2);
    expect(stored()).toEqual({ access: "T2", refresh: "R2" });
    expect(a.store.getState().isAuthenticated).toBe(true);
    expect(b.store.getState().isAuthenticated).toBe(true);
  });

  it("both tabs 401 at the same moment without Web Locks: the refused tab waits for the winner's write and adopts it", async () => {
    const model = installAuthModel();
    // The first refresh (the winner) answers late; the second, refused, at once.
    model.refreshDelays = [150, 0];
    signIn("T1", "R1");
    const a = await openTab();
    const b = await openTab();

    const [ra, rb] = await Promise.all([a.client.get("/thing"), b.client.get("/thing")]);
    expect(ra.data).toEqual({ token: "T2" });
    expect(rb.data).toEqual({ token: "T2" });
    expect(model.refreshCalls).toEqual(["R1", "R1"]);
    expect(stored()).toEqual({ access: "T2", refresh: "R2" });
    expect(a.store.getState().isAuthenticated).toBe(true);
    expect(b.store.getState().isAuthenticated).toBe(true);
    expect(model.logoutCalls).toEqual([]);
  });

  it("B's refresh is in flight when A's completes: B's late refusal adopts what A stored, and storage is never cleared", async () => {
    const model = installAuthModel();
    // A's refresh arrives first (consuming R1) and answers after 40 ms. B's
    // request starts before that answer, so B also posts R1; its refusal
    // answers after 100 ms, when A has already written T2 and R2.
    model.refreshDelays = [40, 100];
    signIn("T1", "R1");
    const a = await openTab();
    const b = await openTab();
    const removeItem = vi.spyOn(Storage.prototype, "removeItem");

    const pa = a.client.get("/thing");
    await sleep(5);
    const pb = b.client.get("/thing");
    const [ra, rb] = await Promise.all([pa, pb]);
    expect(ra.data).toEqual({ token: "T2" });
    expect(rb.data).toEqual({ token: "T2" });
    expect(model.refreshCalls).toEqual(["R1", "R1"]);
    expect(removeItem).not.toHaveBeenCalled();
    expect(stored()).toEqual({ access: "T2", refresh: "R2" });
  });

  it("without Web Locks, a winner whose write lands after the grace leaves every tab consistently signed out and revokes its new pair", async () => {
    const { UNLOCKED_REFUSAL_GRACE_MS } = await import("@/api/client");
    const model = installAuthModel();
    model.refreshDelays = [UNLOCKED_REFUSAL_GRACE_MS + 400, 0];
    signIn("T1", "R1");
    const a = await openTab();
    const b = await openTab();

    const results = await Promise.allSettled([a.client.get("/thing"), b.client.get("/thing")]);
    expect(results.map((r) => r.status)).toEqual(["rejected", "rejected"]);
    // The loser cleared storage; the late winner did not write over the logout.
    expect(stored()).toEqual({ access: null, refresh: null });
    await vi.waitFor(() => expect(model.logoutCalls).toEqual(["R2"]));
    deliverStorageEvent();
    expect(a.store.getState().isAuthenticated).toBe(false);
    expect(b.store.getState().isAuthenticated).toBe(false);
  }, 10000);

  it("A logs out while B's request is in flight: B's 401 sends no refresh and signs B out", async () => {
    const model = installAuthModel();
    model.protectedDelay = 50;
    signIn("T1", "R1");
    const a = await openTab();
    const b = await openTab();

    const pending = b.client.get("/thing");
    await sleep(5);
    a.store.getState().clearAuth();

    await expect(pending).rejects.toMatchObject({ response: { status: 401 } });
    expect(model.refreshCalls).toEqual([]);
    expect(b.store.getState().isAuthenticated).toBe(false);
    expect(stored()).toEqual({ access: null, refresh: null });
  });

  it("A logs out while B's refresh is in flight: the logout wins and B's new pair is revoked, not stored", async () => {
    const model = installAuthModel();
    model.refreshDelays = [60];
    signIn("T1", "R1");
    const a = await openTab();
    const b = await openTab();

    const pending = b.client.get("/thing");
    await sleep(20);
    expect(model.refreshCalls).toEqual(["R1"]);
    a.store.getState().clearAuth();

    await expect(pending).rejects.toThrow("Signed out in another tab");
    expect(stored()).toEqual({ access: null, refresh: null });
    await vi.waitFor(() => expect(model.logoutCalls).toEqual(["R2"]));
    expect(b.store.getState().isAuthenticated).toBe(false);
  });

  it("a genuinely revoked session signs out every tab", async () => {
    const model = installAuthModel([]);
    installFakeWebLocks();
    signIn("T1", "R1");
    const a = await openTab();
    const b = await openTab();

    await expect(a.client.get("/thing")).rejects.toMatchObject({ response: { status: 401 } });
    expect(model.refreshCalls).toEqual(["R1"]);
    expect(stored()).toEqual({ access: null, refresh: null });
    expect(a.store.getState().isAuthenticated).toBe(false);

    deliverStorageEvent();
    expect(b.store.getState().isAuthenticated).toBe(false);
    await expect(b.client.get("/thing")).rejects.toMatchObject({ response: { status: 401 } });
    // B had nothing left to refresh with, so it sent nothing.
    expect(model.refreshCalls).toEqual(["R1"]);
  });

  it("a genuinely revoked session signs out without Web Locks too, after the grace", async () => {
    const model = installAuthModel([]);
    signIn("T1", "R1");
    const a = await openTab();

    await expect(a.client.get("/thing")).rejects.toMatchObject({ response: { status: 401 } });
    expect(model.refreshCalls).toEqual(["R1"]);
    expect(stored()).toEqual({ access: null, refresh: null });
    expect(a.store.getState().isAuthenticated).toBe(false);
  }, 10000);

  it("a refresh that fails without a 401 keeps the shared tokens, and a later request refreshes normally", async () => {
    const model = installAuthModel();
    signIn("T1", "R1");
    const a = await openTab();
    server.use(
      http.post("/api/auth/refresh", () => HttpResponse.json({ detail: "x" }, { status: 503 }), {
        once: true,
      }),
    );

    await expect(a.client.get("/thing")).rejects.toMatchObject({ response: { status: 503 } });
    expect(stored()).toEqual({ access: "T1", refresh: "R1" });
    expect(a.store.getState().isAuthenticated).toBe(true);

    await expect(a.client.get("/thing")).resolves.toMatchObject({ data: { token: "T2" } });
    expect(model.refreshCalls).toEqual(["R1"]);
  });

  it("storage that throws: the tab refreshes from its own memory and still signs out on a refusal", async () => {
    const model = installAuthModel();
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    const a = await openTab();
    expect(a.store.getState().isAuthenticated).toBe(false);
    a.store.getState().setTokens("T1", "R1");

    await expect(a.client.get("/thing")).resolves.toMatchObject({ data: { token: "T2" } });
    expect(a.store.getState().accessToken).toBe("T2");
    expect(model.refreshCalls).toEqual(["R1"]);

    model.validAccess.clear();
    model.live.clear();
    await expect(a.client.get("/thing")).rejects.toMatchObject({ response: { status: 401 } });
    expect(model.refreshCalls).toEqual(["R1", "R2"]);
    expect(a.store.getState().isAuthenticated).toBe(false);
  }, 10000);
});
