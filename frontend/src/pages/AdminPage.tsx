import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuthStore } from "@/stores/authStore";
import { CreateDeviceForm } from "@/components/admin/CreateDeviceForm";
import { UserManagementTable } from "@/components/admin/UserManagementTable";

export function AdminPage() {
  const user = useAuthStore((s) => s.user);
  const navigate = useNavigate();
  const [showAddDevice, setShowAddDevice] = useState(false);

  useEffect(() => {
    if (user && user.role !== "admin" && user.role !== "superadmin") {
      navigate("/topology");
    }
  }, [user, navigate]);

  if (!user || (user.role !== "admin" && user.role !== "superadmin")) {
    return null;
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-4xl mx-auto px-6 py-8 space-y-10">
        <section aria-labelledby="add-device-heading">
          <button
            onClick={() => setShowAddDevice((v) => !v)}
            className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors"
          >
            {showAddDevice ? "Hide Form" : "Add Device"}
          </button>
          {showAddDevice && (
            <div className="bg-white rounded-lg border border-gray-200 p-6 mt-4">
              <CreateDeviceForm />
            </div>
          )}
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
      </div>
    </div>
  );
}
