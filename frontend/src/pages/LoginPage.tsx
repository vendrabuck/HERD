import { useEffect, useId, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import toast from "react-hot-toast";
import { ChevronRight, Wrench } from "lucide-react";
import { useLogin, useLoginOptions } from "@/api/auth";
import { useConfigStatus } from "@/api/config";
import { Button } from "@/components/ui/Button";
import { APP_VERSION } from "@/lib/appVersion";
import { cn } from "@/lib/cn";
import { loginErrorText, returnPathFromState } from "@/lib/login";
import {
  SSO_ERROR_CANCELLED,
  SSO_ERROR_PARAM,
  SSO_LOGIN_URL,
  loginPageView,
  ssoErrorMessage,
} from "@/lib/sso";
import { navigateTopLevel } from "@/lib/topLevelNavigation";
import { tokenSubject } from "@/stores/authStore";

// One toast per sso_error, even when the effect below runs twice (StrictMode).
const SSO_ERROR_TOAST_ID = "sso-error";

export function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const login = useLogin();
  const [form, setForm] = useState({ email: "", password: "" });
  const [adminOpen, setAdminOpen] = useState(false);
  const adminPanelId = useId();
  const { data: configStatus } = useConfigStatus();
  const loginOptions = useLoginOptions();
  // ADR 0016 D10: pending and every error render today's page.
  const view = loginPageView(loginOptions.isSuccess ? loginOptions.data : null);

  const isUnconfigured = configStatus && !configStatus.configured;

  // ADR 0016: auth's single sign-on routes come back to /login?sso_error=...
  // Show the fixed message for it once, never the raw value, then drop the
  // parameter with a replace, so Back and a reload do not show it again.
  const handledSsoError = useRef<string | null>(null);
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const value = params.get(SSO_ERROR_PARAM);
    if (value === null || handledSsoError.current === location.key) return;
    handledSsoError.current = location.key;
    const message = ssoErrorMessage(value);
    if (message === SSO_ERROR_CANCELLED) toast(message, { id: SSO_ERROR_TOAST_ID });
    else toast.error(message, { id: SSO_ERROR_TOAST_ID });
    params.delete(SSO_ERROR_PARAM);
    const search = params.toString();
    navigate(
      { pathname: location.pathname, search: search ? `?${search}` : "", hash: location.hash },
      { replace: true, state: location.state },
    );
  }, [location, navigate]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (isUnconfigured) {
      toast.error("System requires initial configuration");
      return;
    }
    try {
      const tokens = await login.mutateAsync(form);
      navigate(returnPathFromState(location.state, tokenSubject(tokens.access_token)), {
        replace: true,
      });
    } catch (err) {
      toast.error(loginErrorText(err));
    }
  };

  const passwordForm = (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div>
        <label htmlFor="login-email" className="block text-sm font-medium text-gray-700 mb-1">Email</label>
        {/* type="text", not "email": in LDAP mode this is the directory
            login name, which need not contain an @ (IAM-LDAP-2, issue
            #1335). The backend validates the value in both modes. */}
        <input
          id="login-email"
          type="text"
          autoComplete="username"
          inputMode="email"
          autoCapitalize="none"
          spellCheck={false}
          required
          disabled={!!isUnconfigured}
          value={form.email}
          onChange={(e) => setForm({ ...form, email: e.target.value })}
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50 disabled:bg-gray-100"
          placeholder="you@example.com"
          aria-describedby="login-email-help"
        />
        <p id="login-email-help" className="mt-1 text-xs text-gray-500">
          Directory accounts: enter your directory login name.
        </p>
      </div>

      <div>
        <label htmlFor="login-password" className="block text-sm font-medium text-gray-700 mb-1">Password</label>
        <input
          id="login-password"
          type="password"
          autoComplete="current-password"
          required
          disabled={!!isUnconfigured}
          value={form.password}
          onChange={(e) => setForm({ ...form, password: e.target.value })}
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50 disabled:bg-gray-100"
          placeholder="password"
        />
      </div>

      {/* Behind the Administrator sign-in toggle (single sign-on mode) the
          submit is secondary, so the page keeps one primary action, the
          "Sign in with" button; everywhere else it is today's primary. */}
      <Button
        type="submit"
        variant={view.passwordForm === "toggle" ? "secondary" : "primary"}
        disabled={login.isPending || !!isUnconfigured}
        aria-busy={login.isPending}
        className="w-full"
      >
        {login.isPending ? "Signing in..." : "Sign in"}
      </Button>
    </form>
  );

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50">
      <div className="w-full max-w-sm bg-white rounded-xl shadow-sm border border-gray-200 p-8 relative">
        <div className="mb-6 text-center">
          <div className="flex items-center justify-center gap-3">
            <div className="w-12 h-12 rounded-lg bg-slate-800 flex items-center justify-center shrink-0">
              <span className="font-mono font-bold text-2xl text-sky-400">H</span>
            </div>
            <span className="flex items-baseline gap-1.5">
              <h1 className="text-2xl font-bold text-gray-900">HERD</h1>
              <span data-testid="app-version-login" className="text-xs text-gray-400">
                v{APP_VERSION}
              </span>
            </span>
          </div>
          <p className="text-sm text-gray-500 mt-1">Hardware Environment Replication and Deployment</p>
        </div>

        {isUnconfigured && (
          <div className="mb-4 p-3 bg-amber-50 border border-amber-200 rounded-lg text-sm text-amber-800">
            System requires initial configuration. Click the wrench icon to set up HERD.
          </div>
        )}

        {view.ssoLabel !== null && (
          <Button
            variant="primary"
            className="w-full break-words"
            disabled={!!isUnconfigured}
            onClick={() => navigateTopLevel(SSO_LOGIN_URL)}
          >
            {view.ssoLabel}
          </Button>
        )}

        {view.passwordForm === "toggle" && (
          <div className="mt-4 border-t border-gray-200 pt-4">
            <button
              type="button"
              aria-expanded={adminOpen}
              aria-controls={adminPanelId}
              onClick={() => setAdminOpen((open) => !open)}
              className="flex w-full items-center justify-between rounded-lg px-1 py-1 text-sm font-medium text-gray-600 transition-colors hover:text-gray-900 focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              Administrator sign-in
              <ChevronRight
                className={cn("w-4 h-4", adminOpen && "rotate-90")}
                aria-hidden="true"
              />
            </button>
            <div id={adminPanelId} className={adminOpen ? "mt-3" : undefined}>
              {adminOpen && passwordForm}
            </div>
          </div>
        )}

        {view.passwordForm === "shown" && passwordForm}

        {view.registerLink && (
          <p className="text-center text-sm text-gray-500 mt-4">
            No account?{" "}
            <Link to="/register" className="text-blue-600 hover:underline font-medium">
              Register
            </Link>
          </p>
        )}

        <Link
          to="/config"
          className="absolute bottom-4 right-4 p-2 text-gray-400 hover:text-gray-600 transition-colors"
          title="System Configuration"
          aria-label="System configuration"
        >
          <Wrench className="w-5 h-5" aria-hidden="true" />
        </Link>
      </div>
    </div>
  );
}
