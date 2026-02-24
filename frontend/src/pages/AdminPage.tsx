import { useEffect } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuthStore } from "@/stores/authStore";
import { CreateDeviceForm } from "@/components/admin/CreateDeviceForm";
import { UserManagementTable } from "@/components/admin/UserManagementTable";

export function AdminPage() {
  const user = useAuthStore((s) => s.user);
  const navigate = useNavigate();

  useEffect(() => {
    if (user && user.role !== "admin" && user.role !== "superadmin") {
      navigate("/dashboard");
    }
  }, [user, navigate]);

  if (!user || (user.role !== "admin" && user.role !== "superadmin")) {
    return null;
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-gray-900 text-white px-6 py-3 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <span className="font-bold text-lg tracking-tight">HERD</span>
          <span className="text-gray-400 text-sm">Admin</span>
        </div>
        <Link
          to="/dashboard"
          className="text-sm text-gray-400 hover:text-white transition-colors"
        >
          Back to Dashboard
        </Link>
      </header>

      <main className="max-w-4xl mx-auto px-6 py-8 space-y-10">
        <section aria-labelledby="add-device-heading">
          <h2 id="add-device-heading" className="text-lg font-semibold text-gray-900 mb-4">
            Add Device
          </h2>
          <div className="bg-white rounded-lg border border-gray-200 p-6">
            <CreateDeviceForm />
          </div>
        </section>

        {user.role === "superadmin" && (
          <section aria-labelledby="user-mgmt-heading">
            <h2 id="user-mgmt-heading" className="text-lg font-semibold text-gray-900 mb-4">
              User Management
            </h2>
            <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
              <UserManagementTable currentUserId={user.id} />
            </div>
          </section>
        )}
      </main>
    </div>
  );
}
