import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { adoptSignInTokens, exchangeOidcHandoff } from "@/api/auth";
import type { LoginRedirectState } from "@/lib/login";
import { SSO_COMPLETE_PATH, SSO_EXCHANGE_FAILED_PATH, type OidcExchangeResult } from "@/lib/sso";
import { tokenSubject } from "@/stores/authStore";

/**
 * `/login/sso` (ADR 0016 phase 4): auth's callback lands here after the
 * provider signed the person in, with a one-time HttpOnly handoff cookie.
 *
 * The page posts `{}` to the exchange exactly once per mount: the ref holds
 * across React StrictMode's second effect run and across re-renders, so the
 * single-use handoff is never spent twice. On any failure it stores nothing
 * and replaces itself with `/login?sso_error=failed`.
 *
 * On success the answered `next` (already a safe in-app path,
 * `parseOidcExchange`) is first recorded as this entry's return path, the
 * same location state AuthGuard records, bound to the `sub` of the exchanged
 * access token (never null, so GuestGuard honors it for that account only,
 * IAM-UI-13), with a replace; only once that state has committed are the
 * tokens adopted, through the same path a password login takes
 * (`adoptSignInTokens`). GuestGuard then replaces the page with that recorded
 * path. The order matters: the router applies a navigation as a transition
 * while the token store updates synchronously, so adopting first would let
 * GuestGuard redirect on the old state (to `/topology`) before a navigation to
 * `next` landed. An access token whose `sub` cannot be read is a failure.
 *
 * An answer that arrives after the page unmounted (GuestGuard sent the tab
 * away because another tab signed in, say) is dropped: no navigation, no
 * token adoption, no message. The `alive` ref is true while an effect run is
 * mounted; StrictMode's second run sets it again, so the one exchange still
 * completes.
 *
 * No token, cookie, or server text is shown, logged, or put in a URL.
 */
export function OidcCompletePage() {
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const started = useRef(false);
  const alive = useRef(false);
  const adopted = useRef(false);
  const [result, setResult] = useState<OidcExchangeResult | null>(null);

  useEffect(() => {
    alive.current = true;
    if (!started.current) {
      started.current = true;
      exchangeOidcHandoff().then(
        (answer) => {
          if (!alive.current) return;
          const sub = tokenSubject(answer.access_token);
          if (sub === null) {
            navigate(SSO_EXCHANGE_FAILED_PATH, { replace: true });
            return;
          }
          setResult(answer);
          const state: LoginRedirectState = { from: answer.next, sub };
          navigate(SSO_COMPLETE_PATH, { replace: true, state });
        },
        () => {
          if (alive.current) navigate(SSO_EXCHANGE_FAILED_PATH, { replace: true });
        },
      );
    }
    return () => {
      alive.current = false;
    };
  }, [navigate]);

  useEffect(() => {
    if (!result || adopted.current || !alive.current) return;
    const recorded = location.state as { from?: unknown; sub?: unknown } | null;
    if (recorded?.from !== result.next || recorded?.sub !== tokenSubject(result.access_token)) {
      return;
    }
    adopted.current = true;
    adoptSignInTokens(queryClient, result);
  }, [result, location.state, queryClient]);

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50">
      <div className="w-full max-w-sm bg-white rounded-xl shadow-sm border border-gray-200 p-8 text-center">
        <div className="mx-auto mb-4 w-12 h-12 rounded-lg bg-slate-800 flex items-center justify-center">
          <span className="font-mono font-bold text-2xl text-sky-400" aria-hidden="true">
            H
          </span>
        </div>
        <p role="status" className="text-sm text-gray-500">
          Signing in...
        </p>
      </div>
    </div>
  );
}
