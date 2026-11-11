import { QueryClient } from "@tanstack/react-query";
import { useAuthStore } from "@/stores/authStore";
import { usePreferencesStore } from "@/stores/preferencesStore";

/**
 * The app's one TanStack Query client, created here rather than in main.tsx
 * so the sign-out reset below sits beside it and tests can import both.
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: 1,
    },
  },
});

/**
 * The ONE reaction to every sign-out, whatever caused it (issue #1333 review):
 * on each true-to-false edge of `isAuthenticated` (a deliberate logout, a
 * logout in another tab arriving as a storage event, a refused refresh, a
 * whole-storage clear, or a switch to another account) the query cache is
 * emptied and the preferences store reset, so the next account to sign in in
 * this tab never sees the previous account's data. Returns the unsubscribe.
 *
 * No import cycle: this module imports the two stores, and nothing under
 * src/stores or src/api imports it; only main.tsx (and tests) do.
 */
export function resetOnSignOut(client: QueryClient): () => void {
  return useAuthStore.subscribe((state, previous) => {
    if (previous.isAuthenticated && !state.isAuthenticated) {
      client.clear();
      usePreferencesStore.getState().clear();
    }
  });
}

export const stopResetOnSignOut = resetOnSignOut(queryClient);
