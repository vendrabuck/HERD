import { loginRetryAfterSeconds } from "@/lib/errors";

/** Where a sign-in lands when no safe return path was recorded. */
export const DEFAULT_AFTER_LOGIN = "/topology";

// Pages a return path must never name: going back to them after a sign-in
// would bounce through GuestGuard instead of reaching a page.
const NOT_A_RETURN_PATH = new Set(["/login", "/register"]);

// C0 controls, DEL, and C1 controls (issue #1333).
// eslint-disable-next-line no-control-regex
const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f-\u009f]/;

/**
 * The return path `value` names, when it is safe to navigate to, else null
 * (issue #1333). Safe means a same-origin absolute path: a string that starts
 * with exactly one `/`, holds no backslash and no control character, resolves
 * against this origin to this origin, and is not the login or register page.
 * So `//evil.example`, `/\evil.example`, `https://evil.example`,
 * `javascript:...`, and a relative `topology` are all refused.
 */
export function safeReturnPath(value: unknown): string | null {
  if (typeof value !== "string" || value.length === 0) return null;
  if (!value.startsWith("/") || value.startsWith("//")) return null;
  if (value.includes("\\") || CONTROL_CHARACTERS.test(value)) return null;
  let url: URL;
  try {
    url = new URL(value, window.location.origin);
  } catch {
    return null;
  }
  if (url.origin !== window.location.origin) return null;
  if (NOT_A_RETURN_PATH.has(url.pathname)) return null;
  return value;
}

/**
 * The location state AuthGuard records when it sends a signed-out visitor to
 * /login: the path, query, and hash they asked for.
 */
export interface LoginRedirectState {
  from: string;
}

/** Where to go after a sign-in, from the login route's location state. */
export function returnPathFromState(state: unknown): string {
  const from = (state as { from?: unknown } | null | undefined)?.from;
  return safeReturnPath(from) ?? DEFAULT_AFTER_LOGIN;
}

export const LOGIN_INVALID_CREDENTIALS =
  "Invalid credentials. Check the email or username and the password.";
export const LOGIN_UNREACHABLE = "Could not reach HERD. Try again in a moment.";
export const LOGIN_INPUT_REFUSED =
  "HERD could not accept these sign-in details. The email or username can be at most 255 characters and the password at most 72.";
export const LOGIN_UNEXPECTED = "Sign-in failed with an unexpected answer. Try again in a moment.";
export const LOGIN_RATE_LIMITED = "Too many sign-in attempts. Wait a moment, then try again.";

/**
 * The toast for a failed `POST /api/auth/login` (issue #1333). Only a 401 is
 * a credential refusal (IAM-LOGIN-1, IAM-LDAP-5). No answer at all (network
 * error, timeout), a 404 (the gateway has no route while auth is down), and
 * any 5xx (a gateway 502 or 504 included) mean HERD could not be reached. A
 * 422 is the request schema's length limits (IAM-LOGIN-3). Auth sends no 429
 * or 403 on login today; a 429 from anything in front of it gets the rate
 * limit wording (with the `Retry-After` wait when it is a whole number of
 * seconds), and any other status the unexpected-answer wording. No
 * server text is ever shown.
 */
export function loginErrorText(err: unknown): string {
  const status = (err as { response?: { status?: unknown } } | null | undefined)?.response?.status;
  if (typeof status !== "number") return LOGIN_UNREACHABLE;
  if (status === 401) return LOGIN_INVALID_CREDENTIALS;
  if (status === 404 || status >= 500) return LOGIN_UNREACHABLE;
  if (status === 422) return LOGIN_INPUT_REFUSED;
  if (status === 429) {
    const seconds = loginRetryAfterSeconds(err);
    if (seconds === null) return LOGIN_RATE_LIMITED;
    return `Too many sign-in attempts. Try again in ${seconds} ${seconds === 1 ? "second" : "seconds"}.`;
  }
  return LOGIN_UNEXPECTED;
}
