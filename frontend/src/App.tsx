import { useEffect } from "react";
import { BrowserRouter, Navigate, Outlet, Route, Routes, useNavigate } from "react-router-dom";
import { Toaster } from "react-hot-toast";
import { LoginPage } from "@/pages/LoginPage";
import { RegisterPage } from "@/pages/RegisterPage";
import { ConfigPage } from "@/pages/ConfigPage";
import { InventoryPage } from "@/pages/InventoryPage";
import { TopologyPage } from "@/pages/TopologyPage";
import { TopologyEditorPage } from "@/pages/TopologyEditorPage";
import { ReservationsPage } from "@/pages/ReservationsPage";
import { ReservationCalendarPage } from "@/pages/ReservationCalendarPage";
import { ReportingPage } from "@/pages/ReportingPage";
import { AddDevicePage } from "@/pages/admin/AddDevicePage";
import { UsersPage } from "@/pages/admin/UsersPage";
import { GroupsPage } from "@/pages/admin/GroupsPage";
import { GroupDetailPage } from "@/pages/admin/GroupDetailPage";
import { DeviceGroupsPage } from "@/pages/admin/DeviceGroupsPage";
import { DeviceGroupDetailPage } from "@/pages/admin/DeviceGroupDetailPage";
import { DriversPage } from "@/pages/admin/DriversPage";
import { HypervisorsPage } from "@/pages/admin/HypervisorsPage";
import { ConnectionsPage } from "@/pages/admin/ConnectionsPage";
import { GrantsPage } from "@/pages/admin/GrantsPage";
import { LdapSyncPage } from "@/pages/admin/LdapSyncPage";
import { TemplatesPage } from "@/pages/TemplatesPage";
import { TemplateEditorPage } from "@/pages/TemplateEditorPage";
import { TopologyTemplatesPage } from "@/pages/TopologyTemplatesPage";
import { DevicePage } from "@/pages/DevicePage";
import { SettingsPage } from "@/pages/SettingsPage";
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

export function AdminGuard({ children }: { children: React.ReactNode }) {
  const user = useAuthStore((s) => s.user);
  const navigate = useNavigate();
  const isAdmin = user?.role === "admin" || user?.role === "superadmin";

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

export default function App() {
  return (
    <BrowserRouter>
      <Toaster position="top-right" />
      <ErrorBoundary>
      <Routes>
        <Route path="/" element={<Navigate to="/login" replace />} />
        <Route path="/config" element={<ConfigPage />} />
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
                <AppLayout />
            </AuthGuard>
          }
        >
          <Route path="/inventory" element={<InventoryPage />} />
          <Route path="/inventory/:id" element={<DevicePage />} />
          <Route path="/templates" element={<TemplatesPage />} />
          <Route path="/templates/new" element={<TemplateEditorPage />} />
          <Route path="/templates/:id" element={<TemplateEditorPage />} />
          <Route path="/topology" element={<TopologyPage />} />
          <Route path="/topology-templates" element={<TopologyTemplatesPage />} />
          <Route path="/topology/:id" element={<TopologyEditorPage />} />
          <Route path="/reservations/calendar" element={<ReservationCalendarPage />} />
          <Route path="/reservations" element={<ReservationsPage />} />
          <Route path="/reporting" element={<ReportingPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/admin" element={<Navigate to="/admin/add-device" replace />} />
          <Route
            element={
              <AdminGuard>
                <Outlet />
              </AdminGuard>
            }
          >
            <Route path="/admin/add-device" element={<AddDevicePage />} />
            <Route path="/admin/users" element={<UsersPage />} />
            <Route path="/admin/groups" element={<GroupsPage />} />
            <Route path="/admin/groups/new" element={<GroupDetailPage />} />
            <Route path="/admin/groups/:id" element={<GroupDetailPage />} />
            <Route path="/admin/device-groups" element={<DeviceGroupsPage />} />
            <Route path="/admin/device-groups/new" element={<DeviceGroupDetailPage />} />
            <Route path="/admin/device-groups/:id" element={<DeviceGroupDetailPage />} />
            <Route path="/admin/connections" element={<ConnectionsPage />} />
            <Route path="/admin/drivers" element={<DriversPage />} />
            <Route path="/admin/grants" element={<GrantsPage />} />
            <Route path="/admin/hypervisors" element={<HypervisorsPage />} />
            <Route path="/admin/ldap-sync" element={<LdapSyncPage />} />
          </Route>
        </Route>
        <Route path="/dashboard" element={<Navigate to="/topology" replace />} />
        <Route path="*" element={<Navigate to="/topology" replace />} />
      </Routes>
      </ErrorBoundary>
    </BrowserRouter>
  );
}
