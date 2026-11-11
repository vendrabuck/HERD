// Shared harness for the list-page keyboard tests (issue #1317): renders a
// page inside a memory data router and records every navigation, so a test
// can assert that one activation moved the router EXACTLY once (the row
// click handler must not navigate a second time behind the name link).
import type { ReactNode } from "react";
import { render } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryRouter, RouterProvider } from "react-router-dom";

export function renderInCountingRouter(page: ReactNode, listPath: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const router = createMemoryRouter(
    [
      { path: listPath, element: page },
      { path: "*", element: <div data-testid="destination">destination</div> },
    ],
    { initialEntries: [listPath] },
  );
  const navigations: string[] = [];
  let lastKey = router.state.location.key;
  router.subscribe((state) => {
    if (state.location.key !== lastKey) {
      lastKey = state.location.key;
      navigations.push(state.location.pathname);
    }
  });
  const utils = render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { ...utils, router, navigations };
}

/**
 * Presses Tab until `target` has focus, failing after `max` presses. Proves
 * the element is in the sequential focus order, not merely focusable.
 */
export async function tabTo(user: UserEvent, target: HTMLElement, max = 60): Promise<void> {
  for (let i = 0; i < max; i += 1) {
    await user.tab();
    if (document.activeElement === target) return;
  }
  throw new Error(`Tab never reached ${target.outerHTML.slice(0, 120)} in ${max} presses`);
}
