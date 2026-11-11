import { useEffect } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { tokenSubject, useAuthStore } from "@/stores/authStore";
import { isAdminRole } from "@/lib/roles";
import { returnPathFromState, type LoginRedirectState } from "@/lib/login";

// A signed-out visit (first visit, an expired session the 401 interceptor
// cleared, or a logout in another tab) records where the visitor was and
// which account was signed in, so the login page can return the same account
// there (issue #1333).
export function AuthGuard({ children }: { children: React.ReactNode }) {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const lastSubject = useAuthStore((s) => s.lastSubject);
  const location = useLocation();
  if (!isAuthenticated) {
    const state: LoginRedirectState = {
      from: location.pathname + location.search + location.hash,
      sub: lastSubject,
    };
    return <Navigate to="/login" replace state={state} />;
  }
  return <>{children}</>;
}

// Sends a signed-in visitor of /login or /register where LoginPage would: the
// recorded return path when it is safe and recorded for this account, else
// the default page.
export function GuestGuard({ children }: { children: React.ReactNode }) {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const accessToken = useAuthStore((s) => s.accessToken);
  const location = useLocation();
  if (isAuthenticated) {
    return <Navigate to={returnPathFromState(location.state, tokenSubject(accessToken))} replace />;
  }
  return <>{children}</>;
}

export function AdminGuard({ children }: { children: React.ReactNode }) {
  const user = useAuthStore((s) => s.user);
  const navigate = useNavigate();
  const isAdmin = isAdminRole(user?.role);

  useEffect(() => {
    if (user && !isAdmin) {
      navigate("/topology");
    }
  }, [user, isAdmin, navigate]);

  if (!user || !isAdmin) {
    return null;
  }
  return <>{children}</>;
}
