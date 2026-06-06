import { useState } from "react";
import { usePaginatedUsers, useSetUserRole } from "@/api/admin";
import { Pagination } from "@/components/ui/Pagination";

interface UserManagementTableProps {
  currentUserId: string;
  showRoleControls?: boolean;
}

export function UserManagementTable({ currentUserId, showRoleControls = true }: UserManagementTableProps) {
  const [skip, setSkip] = useState(0);
  const limit = 50;
  const { data, isLoading } = usePaginatedUsers(skip, limit);
  const setRole = useSetUserRole();

  const users = data?.items;
  const total = data?.total ?? 0;

  if (isLoading) {
    return <p className="text-sm text-gray-500 py-4">Loading users...</p>;
  }

  if (!users || users.length === 0) {
    return <p className="text-sm text-gray-500 py-4">No users found</p>;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[600px] text-sm text-left">
        <thead className="bg-gray-50 text-gray-600 uppercase text-xs">
          <tr>
            <th className="px-4 py-3">Username</th>
            <th className="px-4 py-3">Email</th>
            <th className="px-4 py-3">Role</th>
            <th className="px-4 py-3">Created</th>
            {showRoleControls && <th className="px-4 py-3">Action</th>}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {users.map((user) => {
            const isSuperadmin = user.role === "superadmin";
            const isSelf = user.id === currentUserId;
            const disabled = isSuperadmin || isSelf || setRole.isPending;

            return (
              <tr key={user.id} className="hover:bg-gray-50">
                <td className="px-4 py-3 font-medium text-gray-900">{user.username}</td>
                <td className="px-4 py-3 text-gray-600">{user.email}</td>
                <td className="px-4 py-3">
                  <span
                    className={`text-xs px-2 py-0.5 rounded font-medium ${
                      user.role === "superadmin"
                        ? "bg-purple-100 text-purple-800"
                        : user.role === "admin"
                        ? "bg-blue-100 text-blue-800"
                        : "bg-gray-100 text-gray-700"
                    }`}
                  >
                    {user.role}
                  </span>
                </td>
                <td className="px-4 py-3 text-gray-500">
                  {new Date(user.created_at).toLocaleDateString()}
                </td>
                {showRoleControls && (
                  <td className="px-4 py-3">
                    {!isSuperadmin && !isSelf && (
                      <button
                        onClick={() =>
                          setRole.mutate({
                            userId: user.id,
                            role: user.role === "admin" ? "user" : "admin",
                          })
                        }
                        disabled={disabled}
                        className="text-xs text-blue-600 hover:text-blue-800 disabled:opacity-50"
                      >
                        {user.role === "admin" ? "Demote to user" : "Promote to admin"}
                      </button>
                    )}
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
      <Pagination total={total} skip={skip} limit={limit} onPageChange={setSkip} />
    </div>
  );
}
