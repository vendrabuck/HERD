import { http, HttpResponse } from "msw";
import type { AxiosInstance } from "axios";
import { server } from "../mocks/server";
import { fakeJwt } from "../fixtures/jwt";

/** Alice's n-th access token (JWT-shaped, so the store can read its account). */
const T = (n: number) => fakeJwt("alice", n);

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
  logout: typeof import("@/api/client").logoutUnderRefreshLock;
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
    logout: clientModule.logoutUnderRefreshLock,
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
        pair = { access_token: T(minted), refresh_token: `R${minted}` };
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

/**
 * A fake LockManager shared by every tab: one FIFO exclusive lock per name.
 * Like the real one it takes `request(name, callback)` or
 * `request(name, { signal }, callback)`, and a request still waiting when its
 * signal aborts rejects with an AbortError and never runs its callback.
 */
function installFakeWebLocks() {
  const tails = new Map<string, Promise<unknown>>();
  const fake = {
    grants: 0,
    aborted: 0,
    request<T>(
      name: string,
      optionsOrCallback: { signal?: AbortSignal } | (() => Promise<T>),
      maybeCallback?: () => Promise<T>,
    ): Promise<T> {
      const callback =
        typeof optionsOrCallback === "function" ? optionsOrCallback : maybeCallback!;
      const signal = typeof optionsOrCallback === "function" ? undefined : optionsOrCallback.signal;
      const previous = tails.get(name) ?? Promise.resolve();
      let abortedFirst = false;
      let isGranted = false;
      const aborted = new Promise<never>((_, reject) => {
        signal?.addEventListener("abort", () => {
          if (isGranted) return;
          abortedFirst = true;
          fake.aborted += 1;
          reject(new DOMException("aborted", "AbortError"));
        });
      });
      const granted = previous.then(() => {
        if (abortedFirst) throw new DOMException("aborted", "AbortError");
        isGranted = true;
        fake.grants += 1;
        return callback();
      });
      tails.set(
        name,
        granted.catch(() => undefined),
      );
      return signal ? Promise.race([granted, aborted]) : granted;
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
    signIn(T(1), "R1");
    const a = await openTab();
    const b = await openTab();

    await expect(a.client.get("/thing")).resolves.toMatchObject({ data: { token: T(2) } });
    // No storage event reaches B: its memory still holds T1 and R1.
    expect(b.store.getState().accessToken).toBe(T(1));

    await expect(b.client.get("/thing")).resolves.toMatchObject({ data: { token: T(2) } });
    expect(model.refreshCalls).toEqual(["R1"]);
    expect(b.store.getState().accessToken).toBe(T(2));
    expect(b.store.getState().refreshToken).toBe("R2");
    expect(stored()).toEqual({ access: T(2), refresh: "R2" });
  });

  it("A refreshes and the storage event reaches B: B's next request carries the new token at once", async () => {
    const model = installAuthModel();
    signIn(T(1), "R1");
    const a = await openTab();
    const b = await openTab();

    await a.client.get("/thing");
    deliverStorageEvent();
    expect(b.store.getState().accessToken).toBe(T(2));

    await expect(b.client.get("/thing")).resolves.toMatchObject({ data: { token: T(2) } });
    expect(model.refreshCalls).toEqual(["R1"]);
  });

  it("both tabs 401 at the same moment with Web Locks: one refresh serves both", async () => {
    const model = installAuthModel();
    model.refreshDelays = [30];
    const locks = installFakeWebLocks();
    signIn(T(1), "R1");
    const a = await openTab();
    const b = await openTab();

    const [ra, rb] = await Promise.all([a.client.get("/thing"), b.client.get("/thing")]);
    expect(ra.data).toEqual({ token: T(2) });
    expect(rb.data).toEqual({ token: T(2) });
    expect(model.refreshCalls).toEqual(["R1"]);
    expect(locks.grants).toBe(2);
    expect(stored()).toEqual({ access: T(2), refresh: "R2" });
    expect(a.store.getState().isAuthenticated).toBe(true);
    expect(b.store.getState().isAuthenticated).toBe(true);
  });

  it("both tabs 401 at the same moment without Web Locks: the refused tab waits for the winner's write and adopts it", async () => {
    const model = installAuthModel();
    // The first refresh (the winner) answers late; the second, refused, at once.
    model.refreshDelays = [150, 0];
    signIn(T(1), "R1");
    const a = await openTab();
    const b = await openTab();

    const [ra, rb] = await Promise.all([a.client.get("/thing"), b.client.get("/thing")]);
    expect(ra.data).toEqual({ token: T(2) });
    expect(rb.data).toEqual({ token: T(2) });
    expect(model.refreshCalls).toEqual(["R1", "R1"]);
    expect(stored()).toEqual({ access: T(2), refresh: "R2" });
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
    signIn(T(1), "R1");
    const a = await openTab();
    const b = await openTab();
    const removeItem = vi.spyOn(Storage.prototype, "removeItem");

    const pa = a.client.get("/thing");
    await sleep(5);
    const pb = b.client.get("/thing");
    const [ra, rb] = await Promise.all([pa, pb]);
    expect(ra.data).toEqual({ token: T(2) });
    expect(rb.data).toEqual({ token: T(2) });
    expect(model.refreshCalls).toEqual(["R1", "R1"]);
    expect(removeItem).not.toHaveBeenCalled();
    expect(stored()).toEqual({ access: T(2), refresh: "R2" });
  });

  it("without Web Locks, a winner whose write lands after the grace leaves every tab consistently signed out and revokes its new pair", async () => {
    const { REFUSAL_GRACE_MS } = await import("@/api/client");
    const model = installAuthModel();
    model.refreshDelays = [REFUSAL_GRACE_MS + 400, 0];
    signIn(T(1), "R1");
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
    signIn(T(1), "R1");
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
    signIn(T(1), "R1");
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
    signIn(T(1), "R1");
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
  }, 10000);

  it("a genuinely revoked session signs out without Web Locks too, after the grace", async () => {
    const model = installAuthModel([]);
    signIn(T(1), "R1");
    const a = await openTab();

    await expect(a.client.get("/thing")).rejects.toMatchObject({ response: { status: 401 } });
    expect(model.refreshCalls).toEqual(["R1"]);
    expect(stored()).toEqual({ access: null, refresh: null });
    expect(a.store.getState().isAuthenticated).toBe(false);
  }, 10000);

  it("a refresh that fails without a 401 keeps the shared tokens, and a later request refreshes normally", async () => {
    const model = installAuthModel();
    signIn(T(1), "R1");
    const a = await openTab();
    server.use(
      http.post("/api/auth/refresh", () => HttpResponse.json({ detail: "x" }, { status: 503 }), {
        once: true,
      }),
    );

    await expect(a.client.get("/thing")).rejects.toMatchObject({ response: { status: 503 } });
    expect(stored()).toEqual({ access: T(1), refresh: "R1" });
    expect(a.store.getState().isAuthenticated).toBe(true);

    await expect(a.client.get("/thing")).resolves.toMatchObject({ data: { token: T(2) } });
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
    a.store.getState().setTokens(T(1), "R1");

    await expect(a.client.get("/thing")).resolves.toMatchObject({ data: { token: T(2) } });
    expect(a.store.getState().accessToken).toBe(T(2));
    expect(model.refreshCalls).toEqual(["R1"]);

    model.validAccess.clear();
    model.live.clear();
    await expect(a.client.get("/thing")).rejects.toMatchObject({ response: { status: 401 } });
    expect(model.refreshCalls).toEqual(["R1", "R2"]);
    expect(a.store.getState().isAuthenticated).toBe(false);
  }, 10000);

  it("with Web Locks, a lock holder that still reads the refused token waits for the other tab's write and adopts it", async () => {
    // Another tab already rotated R1 into R2 at auth, but its storage write
    // has not reached this tab's view yet when this tab holds the lock.
    const model = installAuthModel(["R2"]);
    model.validAccess.add(T(2));
    installFakeWebLocks();
    signIn(T(1), "R1");
    const a = await openTab();

    const pending = a.client.get("/thing");
    await sleep(100);
    expect(model.refreshCalls).toEqual(["R1"]);
    signIn(T(2), "R2");

    await expect(pending).resolves.toMatchObject({ data: { token: T(2) } });
    expect(stored()).toEqual({ access: T(2), refresh: "R2" });
    expect(a.store.getState().isAuthenticated).toBe(true);
  }, 10000);

  it("a tab whose lock wait times out goes on without the lock instead of waiting forever", async () => {
    const { LOCK_WAIT_TIMEOUT_MS } = await import("@/api/client");
    const model = installAuthModel();
    const locks = installFakeWebLocks();
    signIn(T(1), "R1");
    const a = await openTab();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      // A frozen tab holds the lock and never lets go.
      void locks.request("herd-auth-refresh", () => new Promise<never>(() => {}));
      const pending = a.client.get("/thing");
      await vi.advanceTimersByTimeAsync(LOCK_WAIT_TIMEOUT_MS - 1000);
      expect(model.refreshCalls).toEqual([]);
      await vi.advanceTimersByTimeAsync(2000);
      await expect(pending).resolves.toMatchObject({ data: { token: T(2) } });
      expect(model.refreshCalls).toEqual(["R1"]);
      expect(locks.aborted).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("a request is retried at most once: a retry that answers 500 is not retried again", async () => {
    installAuthModel();
    signIn(T(1), "R1");
    const a = await openTab();
    let hits = 0;
    server.use(
      http.get("/api/once", () => {
        hits += 1;
        return hits === 1
          ? HttpResponse.json({ detail: "expired" }, { status: 401 })
          : HttpResponse.json({ detail: "boom" }, { status: 500 });
      }),
    );
    await expect(a.client.get("/once")).rejects.toMatchObject({ response: { status: 500 } });
    expect(hits).toBe(2);
  });

  it("a request is retried at most once: a retry that answers 401 again is rejected without another refresh", async () => {
    const model = installAuthModel();
    signIn(T(1), "R1");
    const a = await openTab();
    let hits = 0;
    server.use(
      http.get("/api/once", () => {
        hits += 1;
        return HttpResponse.json({ detail: "expired" }, { status: 401 });
      }),
    );
    await expect(a.client.get("/once")).rejects.toMatchObject({ response: { status: 401 } });
    expect(hits).toBe(2);
    expect(model.refreshCalls).toEqual(["R1"]);
  });

  it("a 401 while storage holds another account's token signs this tab out and never sends that token", async () => {
    const model = installAuthModel();
    signIn(T(1), "R1");
    const a = await openTab();
    const seen: string[] = [];
    server.use(
      http.get("/api/thing", ({ request }) => {
        seen.push(request.headers.get("Authorization") ?? "");
        return HttpResponse.json({ detail: "expired" }, { status: 401 });
      }),
    );
    // Another tab logged out and bob logged in; this tab saw no event yet.
    signIn(fakeJwt("bob", 1), "RB");

    await expect(a.client.get("/thing")).rejects.toMatchObject({ response: { status: 401 } });
    expect(seen).toEqual([`Bearer ${T(1)}`]);
    expect(model.refreshCalls).toEqual([]);
    expect(a.store.getState().accessToken).toBeNull();
    // Bob's session in storage is untouched, and the tab adopts it next.
    expect(stored()).toEqual({ access: fakeJwt("bob", 1), refresh: "RB" });
    await vi.waitFor(() => expect(a.store.getState().accessToken).toBe(fakeJwt("bob", 1)));
  });

  it("storage that fails a write: the tab keeps its new pair in memory and never adopts the older stored one", async () => {
    const model = installAuthModel();
    signIn(T(1), "R1");
    const a = await openTab();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });

    await expect(a.client.get("/thing")).resolves.toMatchObject({ data: { token: T(2) } });
    expect(stored()).toEqual({ access: T(1), refresh: "R1" });
    expect(a.store.getState().refreshToken).toBe("R2");

    // T2 expires: the tab refreshes with R2 from memory, not T1 or R1 from storage.
    model.validAccess.delete(T(2));
    await expect(a.client.get("/thing")).resolves.toMatchObject({ data: { token: T(3) } });
    expect(model.refreshCalls).toEqual(["R1", "R2"]);
    expect(a.store.getState().isAuthenticated).toBe(true);
  });

  it("logout waits for another tab's refresh holding the lock and revokes the token that refresh stored, not this tab's stale one", async () => {
    const model = installAuthModel();
    model.refreshDelays = [80];
    installFakeWebLocks();
    signIn(T(1), "R1");
    const a = await openTab();
    const b = await openTab();

    const refreshing = b.client.get("/thing");
    await sleep(20);
    expect(model.refreshCalls).toEqual(["R1"]);
    // A never saw B's write (no storage event): its memory still holds R1.
    const sent: string[] = [];
    await a.logout(async (token) => {
      sent.push(token);
      model.live.delete(token);
    });
    await expect(refreshing).resolves.toMatchObject({ data: { token: T(2) } });
    expect(sent).toEqual(["R2"]);
    expect(model.live.has("R2")).toBe(false);
    expect(stored()).toEqual({ access: null, refresh: null });
    expect(a.store.getState().isAuthenticated).toBe(false);
  });
});
