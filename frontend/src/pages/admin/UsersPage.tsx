import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useAuthStore } from "@/stores/authStore";
import { UserManagementTable } from "@/components/admin/UserManagementTable";

export function UsersPage() {
  const user = useAuthStore((s) => s.user);
  const navigate = useNavigate();
  const isAdmin = user?.role === "admin" || user?.role === "superadmin";
  const isSuperadmin = user?.role === "superadmin";

  useEffect(() => {
    if (user && !isAdmin) {
      navigate("/topology");
    }
  }, [user, isAdmin, navigate]);

  if (!user || !isAdmin) {
    return null;
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="px-6 xl:px-12 2xl:px-16 py-8">
        <section aria-labelledby="user-mgmt-heading">
          <h2 id="user-mgmt-heading" className="text-lg font-semibold text-gray-900 mb-4">
            User Management
          </h2>
          <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
            <UserManagementTable currentUserId={user.id} showRoleControls={isSuperadmin} />
          </div>
        </section>
      </div>
    </div>
  );
}
