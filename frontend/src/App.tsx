import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { Toaster } from "react-hot-toast";
import { LoginPage } from "@/pages/LoginPage";
import { RegisterPage } from "@/pages/RegisterPage";
import { DashboardPage } from "@/pages/DashboardPage";
import { AdminPage } from "@/pages/AdminPage";
import { ErrorBoundary } from "@/components/ui/ErrorBoundary";
import { useAuthStore } from "@/stores/authStore";

function AuthGuard({ children }: { children: React.ReactNode }) {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }
  return <>{children}</>;
}

function GuestGuard({ children }: { children: React.ReactNode }) {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  if (isAuthenticated) {
    return <Navigate to="/dashboard" replace />;
  }
  return <>{children}</>;
}

export default function App() {
  return (
    <BrowserRouter>
      <Toaster position="top-right" />
      <Routes>
        <Route
          path="/login"
          element={
            <GuestGuard>
              <LoginPage />
            </GuestGuard>
          }
        />
        <Route
          path="/register"
          element={
            <GuestGuard>
              <RegisterPage />
            </GuestGuard>
          }
        />
        <Route
          path="/dashboard"
          element={
            <AuthGuard>
              <ErrorBoundary>
                <DashboardPage />
              </ErrorBoundary>
            </AuthGuard>
          }
        />
        <Route
          path="/admin"
          element={
            <AuthGuard>
              <ErrorBoundary>
                <AdminPage />
              </ErrorBoundary>
            </AuthGuard>
          }
        />
        <Route path="*" element={<Navigate to="/dashboard" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
