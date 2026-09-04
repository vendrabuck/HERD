// Route table lifted out of App.tsx so tests can inspect it via
// createRoutesFromElements (issue #551); this JSX-constant shape is an
// interim bridge, not a pattern to copy for other element trees.
import { Navigate, Outlet, Route } from "react-router-dom";
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
import { PurposeReviewPage } from "@/pages/admin/PurposeReviewPage";
import { TemplatesPage } from "@/pages/TemplatesPage";
import { TemplateEditorPage } from "@/pages/TemplateEditorPage";
import { TopologyTemplatesPage } from "@/pages/TopologyTemplatesPage";
import { DevicePage } from "@/pages/DevicePage";
import { SettingsPage } from "@/pages/SettingsPage";
import { AppLayout } from "@/components/layout/AppLayout";
import { AuthGuard, GuestGuard, AdminGuard } from "@/components/guards";

export const appRouteElements = (
  <>
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
      <Route path="/settings" element={<SettingsPage />} />
      <Route path="/admin" element={<Navigate to="/admin/add-device" replace />} />
      <Route
        element={
          <AdminGuard>
            <Outlet />
          </AdminGuard>
        }
      >
        <Route path="/reporting" element={<ReportingPage />} />
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
        <Route path="/admin/purpose-review" element={<PurposeReviewPage />} />
      </Route>
    </Route>
    <Route path="/dashboard" element={<Navigate to="/topology" replace />} />
    <Route path="*" element={<Navigate to="/topology" replace />} />
  </>
);
