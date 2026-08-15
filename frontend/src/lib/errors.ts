/**
 * A HERD backend service returns error detail as either a plain string or,
 * on a pydantic validation failure, a list of error objects. Passing that
 * list straight to toast.error renders raw objects as a React child and
 * throws inside the Toaster, which sits outside the ErrorBoundary in
 * App.tsx and blanks the app. Only ever surface a string; anything else
 * (a list, undefined, a non-string) falls back to the caller-supplied
 * message.
 *
 * Single-sourced here (previously duplicated across HypervisorsPage,
 * GrantsPage, RecipeDraftPanel, api/reservations.ts, and api/topologies.ts)
 * so the guard cannot silently regress in a new call site.
 */
export function errorDetail(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === "string" ? detail : fallback;
}
