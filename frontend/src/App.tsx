import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { Toaster } from "react-hot-toast";
import { LoginPage } from "@/pages/LoginPage";
import { RegisterPage } from "@/pages/RegisterPage";
import { InventoryPage } from "@/pages/InventoryPage";
import { TopologyPage } from "@/pages/TopologyPage";
import { TopologyEditorPage } from "@/pages/TopologyEditorPage";
import { ReservationsPage } from "@/pages/ReservationsPage";
import { ReportingPage } from "@/pages/ReportingPage";
import { AdminPage } from "@/pages/AdminPage";
import { AppLayout } from "@/components/layout/AppLayout";
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
    return <Navigate to="/topology" replace />;
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
          element={
            <AuthGuard>
              <ErrorBoundary>
                <AppLayout />
              </ErrorBoundary>
            </AuthGuard>
          }
        >
          <Route path="/inventory" element={<InventoryPage />} />
          <Route path="/topology" element={<TopologyPage />} />
          <Route path="/topology/:id" element={<TopologyEditorPage />} />
          <Route path="/reservations" element={<ReservationsPage />} />
          <Route path="/reporting" element={<ReportingPage />} />
          <Route path="/admin" element={<AdminPage />} />
        </Route>
        <Route path="/dashboard" element={<Navigate to="/topology" replace />} />
        <Route path="*" element={<Navigate to="/topology" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
