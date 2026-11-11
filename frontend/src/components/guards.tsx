import { useEffect } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { useAuthStore } from "@/stores/authStore";
import { isAdminRole } from "@/lib/roles";
import { returnPathFromState, type LoginRedirectState } from "@/lib/login";

// A signed-out visit (first visit, an expired session the 401 interceptor
// cleared, or a logout in another tab) records where the visitor was, so the
// login page can return there (issue #1333).
export function AuthGuard({ children }: { children: React.ReactNode }) {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const location = useLocation();
  if (!isAuthenticated) {
    const state: LoginRedirectState = {
      from: location.pathname + location.search + location.hash,
    };
    return <Navigate to="/login" replace state={state} />;
  }
  return <>{children}</>;
}

// Sends a signed-in visitor of /login or /register where LoginPage would: the
// recorded return path when it is safe, else the default page.
export function GuestGuard({ children }: { children: React.ReactNode }) {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const location = useLocation();
  if (isAuthenticated) {
    return <Navigate to={returnPathFromState(location.state)} replace />;
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
