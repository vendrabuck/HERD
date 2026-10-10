import { act } from "@testing-library/react";

/**
 * Wait long enough for work a click may have started to become observable
 * before asserting that something did NOT happen (issue #1142).
 *
 * A mutation or fetch started by a click reaches the MSW handler only after
 * several async hops (the mutation queue, the axios interceptors, the request
 * interceptor). An absence check read synchronously right after the click
 * therefore passes even when the click DID start the request, so every "issues
 * no delete" style assertion must await this first. The default is the window
 * the sweep's verifier proved catches a Cancel that also fires the delete.
 */
export async function flushPending(ms = 100): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, ms));
  });
}
